"""The user-facing connection between a conversation and the agent harness."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from ..agent.contracts import AgentRunResult
from ..agent.harness import AgentHarness
from ..agent.policy import RetrievalMode
from ..core.errors import ErrorCode, RavenError
from ..core.events import Event, EventType
from ..core.operations import (
    Operation,
    OperationManager,
    OperationStatus,
    OperationTask,
    OperationTaskRecord,
    OperationType,
)
from ..data_management.conversation_manager import Conversation



class SessionRetryInput(BaseModel):
    """Durable inputs required to retry one complete session turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conversation_id: str
    turn_id: UUID
    user_query: str
    retrieval_mode: RetrievalMode | None
    max_iterations: int
    top_k: int
    memory_token_limit: int
    memory_top_k: int



class SessionRun:
    """Replayable handle for one complete session turn."""

    def __init__(self, task: OperationTask, turn_id: UUID, release: Any) -> None:
        self._task = task
        self._turn_id = turn_id
        self._release = release
        self._released = False


    @property
    def operation_id(self) -> UUID:
        return self._task.operation_id


    @property
    def status(self) -> OperationStatus:
        return self._task.status


    @property
    def task(self) -> OperationTask:
        return self._task


    @property
    def turn_id(self) -> UUID:
        return self._turn_id


    @property
    def stream(self) -> AsyncIterator[Event]:
        return self.events()


    async def events(self, after_event_id: int = 0) -> AsyncIterator[Event]:
        """Yield events from this session task and its descendants."""
        async for event in self._task.events(after_event_id=after_event_id):
            yield event


    async def collect(self) -> AgentRunResult:
        try:
            result = await self._task.result()
            if not isinstance(result, AgentRunResult):
                raise RavenError(
                    ErrorCode.INTERNAL_ERROR,
                    "Session operation returned an invalid result.",
                )
            return result
        finally:
            if self._task.is_finished:
                self._release_once()


    async def cancel(self) -> None:
        try:
            await self._task.cancel()
            try:
                await self._task.result()
            except Exception:
                pass
        finally:
            if self._task.is_finished:
                self._release_once()


    async def wait(self) -> OperationStatus:
        try:
            await self._task.result()
        except Exception:
            pass
        finally:
            if self._task.is_finished:
                self._release_once()
        return self._task.status


    def _release_once(self) -> None:
        if self._released:
            return
        self._released = True
        self._release(self)



class Session:
    """Connect one conversation to one shared agent harness."""

    def __init__(
        self,
        conversation: Conversation,
        harness: AgentHarness,
        operation_manager: OperationManager,
        *,
        llm: Any,
        embed_model: Any,
        memory_token_limit: int = 4000,
        memory_top_k: int = 5,
    ) -> None:
        if llm is None:
            raise ValueError("llm is required")
        if embed_model is None:
            raise ValueError("embed_model is required")
        if memory_token_limit <= 0:
            raise ValueError("memory_token_limit must be positive")
        if memory_top_k <= 0:
            raise ValueError("memory_top_k must be positive")

        self.conversation = conversation
        self.harness = harness
        self._operation_manager = operation_manager
        self._llm = llm
        self._embed_model = embed_model
        self._memory_token_limit = memory_token_limit
        self._memory_top_k = memory_top_k
        self._session_id = uuid4()
        self._owns_conversation = False
        self._turn_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._active_runs: set[SessionRun] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._cleanup_complete = False
        self._started = False
        self._closed = False


    @property
    def conversation_id(self) -> str:
        return self.conversation.conversation_id


    @property
    def is_started(self) -> bool:
        return self._started and not self._closed


    @property
    def is_closed(self) -> bool:
        return self._closed


    def matches_configuration(
        self,
        *,
        llm: Any,
        embed_model: Any,
        max_iterations: int,
        top_k: int,
        memory_token_limit: int,
        memory_top_k: int,
    ) -> bool:
        """Return whether requested settings match this canonical session."""
        return (
            self._llm is llm
            and self._embed_model is embed_model
            and self.harness.max_iterations == max_iterations
            and self.harness.top_k == top_k
            and self._memory_token_limit == memory_token_limit
            and self._memory_top_k == memory_top_k
        )


    async def start(self) -> None:
        """Open the conversation and initialize its memory resources."""
        async with self._lifecycle_lock:
            if self.is_started:
                return
            if self._closed:
                raise RavenError(
                    ErrorCode.CONVERSATION_CLOSED,
                    f"Session for conversation '{self.conversation_id}' is closed.",
                )

            await self.conversation._claim_session(self._session_id)
            self._owns_conversation = True
            try:
                await self.conversation.start()
                await self.conversation.initialize_memory(
                    self._llm,
                    self._embed_model,
                    token_limit=self._memory_token_limit,
                    memory_top_k=self._memory_top_k,
                )
                self._started = True
            except BaseException:
                await self.conversation._release_session(self._session_id)
                self._owns_conversation = False
                raise


    async def close(self) -> None:
        """Cancel active turns without closing shared manager resources."""
        async with self._lifecycle_lock:
            if self._cleanup_complete:
                return
            if self._close_task is None:
                self._closed = True
                self._started = False
                active_runs = list(self._active_runs)
                self._close_task = asyncio.create_task(
                    self._finish_close(active_runs),
                    name=f"raven-session-close-{self.conversation_id}",
                )
            close_task = self._close_task

        try:
            await asyncio.shield(close_task)
        finally:
            if close_task.done():
                async with self._lifecycle_lock:
                    if self._close_task is close_task:
                        self._close_task = None
                        if not close_task.cancelled() and close_task.exception() is None:
                            self._cleanup_complete = True


    async def _finish_close(self, active_runs: list[SessionRun]) -> None:
        """Attempt every cleanup step and report the first failure afterward."""
        cleanup_errors: list[BaseException] = []
        await asyncio.gather(
            *(run.cancel() for run in active_runs),
            return_exceptions=True,
        )

        release_memory = getattr(self.conversation, "release_memory_resources", None)
        if callable(release_memory):
            try:
                await release_memory()
            except BaseException as exc:
                cleanup_errors.append(exc)

        if self._owns_conversation:
            try:
                await self.conversation._release_session(self._session_id)
            except BaseException as exc:
                cleanup_errors.append(exc)
            else:
                self._owns_conversation = False

        if cleanup_errors:
            raise cleanup_errors[0]


    async def generate_response(
        self,
        user_query: str,
        *,
        retrieval_mode: RetrievalMode | str | None = None,
        operation: Operation | None = None,
        retry_of: OperationTaskRecord | None = None,
    ) -> SessionRun:
        """Start one serialized autonomous turn in a new or supplied operation."""
        self._ensure_started()
        if retry_of is None:
            query = user_query.strip() if isinstance(user_query, str) else ""
            if not query:
                raise RavenError(
                    ErrorCode.INVALID_METADATA,
                    "user_query must be a non-empty string.",
                )
            selected_mode = self._normalize_retrieval_mode(retrieval_mode)
            retry_input = SessionRetryInput(
                conversation_id=self.conversation_id,
                turn_id=uuid4(),
                user_query=query,
                retrieval_mode=selected_mode,
                max_iterations=self.harness.max_iterations,
                top_k=self.harness.top_k,
                memory_token_limit=self._memory_token_limit,
                memory_top_k=self._memory_top_k,
            )
        else:
            try:
                retry_input = SessionRetryInput.model_validate(retry_of.retry_input)
            except ValidationError as exc:
                raise RavenError(
                    ErrorCode.INVALID_RETRY_INPUT,
                    "The session retry input is invalid.",
                ) from exc
            if retry_input.conversation_id != self.conversation_id:
                raise RavenError(
                    ErrorCode.INVALID_RETRY_INPUT,
                    "The session retry belongs to a different conversation.",
                )

        query = retry_input.user_query
        selected_mode = retry_input.retrieval_mode
        turn_id = retry_input.turn_id

        await self._turn_lock.acquire()
        if self._closed:
            self._turn_lock.release()
            raise RavenError(
                ErrorCode.CONVERSATION_CLOSED,
                f"Session for conversation '{self.conversation_id}' is closed.",
            )

        run_holder: dict[str, SessionRun] = {}

        async def worker(active_operation: Operation) -> AgentRunResult:
            try:
                existing = await self.conversation.get_turn(turn_id)
                if existing is not None:
                    if existing["user_query"] != query:
                        raise RavenError(
                            ErrorCode.CONVERSATION_TURN_CONFLICT,
                            f"Turn '{turn_id}' is already assigned to a different query.",
                        )
                    stored_result = existing.get("result")
                    if not isinstance(stored_result, dict):
                        raise RavenError(
                            ErrorCode.CONVERSATION_TURN_RESULT_MISSING,
                            f"Turn '{turn_id}' has no stored run result.",
                        )
                    try:
                        result = AgentRunResult.model_validate(
                            stored_result
                        ).model_copy(
                            update={"operation_id": active_operation.operation_id}
                        )
                    except ValidationError as exc:
                        raise RavenError(
                            ErrorCode.CONVERSATION_TURN_RESULT_MISSING,
                            f"Turn '{turn_id}' has an invalid stored run result.",
                        ) from exc
                    reconcile_task = await self.conversation.reconcile_turn(
                        turn_id,
                        operation=active_operation,
                    )
                    await reconcile_task.result()
                    await active_operation.publish(
                        Event(
                            type=EventType.CHAT_RESULT_REUSED,
                            data={
                                "conversation_id": self.conversation_id,
                                "turn_id": str(turn_id),
                                "result": result.model_dump(mode="json"),
                            },
                        )
                    )
                else:
                    agent_run = await self.harness.start(
                        self.conversation,
                        query,
                        retrieval_mode=selected_mode,
                        operation=active_operation,
                    )
                    result = await agent_run.collect()
                    append_task = await self.conversation.append_turn(
                        agent_run.conversation_messages(),
                        turn_id=turn_id,
                        user_query=query,
                        result=result.model_dump(mode="json"),
                        operation=active_operation,
                    )
                    await append_task.result()

                generate_title = getattr(self.conversation, "generate_title", None)
                if generate_title is not None:
                    try:
                        title_task = await generate_title(
                            self._llm,
                            query,
                            operation=active_operation,
                        )
                        await title_task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                return result
            finally:
                run = run_holder.get("run")
                if run is not None:
                    run._release_once()

        try:
            active_operation = operation or await self._operation_manager.create(
                OperationType.SESSION_GENERATE_RESPONSE
            )
            task = await active_operation.run(
                OperationType.SESSION_GENERATE_RESPONSE,
                worker,
                retry_input=retry_input.model_dump(mode="json"),
                retry_of=retry_of,
            )
            session_run = SessionRun(task, turn_id, self._release_run)
            run_holder["run"] = session_run
            self._active_runs.add(session_run)
            if task.is_finished:
                session_run._release_once()
            return session_run
        except BaseException:
            if self._turn_lock.locked():
                self._turn_lock.release()
            raise


    @staticmethod
    def _normalize_retrieval_mode(
        retrieval_mode: RetrievalMode | str | None,
    ) -> RetrievalMode | None:
        if retrieval_mode is None or retrieval_mode == "auto":
            return None
        try:
            return (
                retrieval_mode
                if isinstance(retrieval_mode, RetrievalMode)
                else RetrievalMode(retrieval_mode)
            )
        except ValueError as exc:
            raise RavenError(
                ErrorCode.INVALID_RETRIEVAL_MODE,
                f"Unknown retrieval mode '{retrieval_mode}'.",
            ) from exc


    def _release_run(self, run: SessionRun) -> None:
        if run not in self._active_runs:
            return
        self._active_runs.discard(run)
        if self._turn_lock.locked():
            self._turn_lock.release()


    def _ensure_started(self) -> None:
        if self._closed:
            raise RavenError(
                ErrorCode.CONVERSATION_CLOSED,
                f"Session for conversation '{self.conversation_id}' is closed.",
            )
        if not self._started:
            raise RavenError(
                ErrorCode.CONVERSATION_NOT_STARTED,
                f"Session for conversation '{self.conversation_id}' has not been started.",
            )
