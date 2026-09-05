"""The user-facing connection between a conversation and the agent harness."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from ..agent.contracts import AgentRunResult
from ..agent.harness import AgentHarness
from ..agent.policy import RetrievalMode
from ..core.errors import ErrorCode, RavenError
from ..core.events import Event
from ..core.operations import (
    Operation,
    OperationManager,
    OperationStatus,
    OperationTask,
    OperationType,
)
from ..data_management.conversation_manager import Conversation



class SessionRun:
    """Replayable handle for one complete session turn."""

    def __init__(self, task: OperationTask, release: Any) -> None:
        self._task = task
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
    def stream(self) -> AsyncIterator[Event]:
        return self.events()


    async def events(self, after_event_id: int = 0) -> AsyncIterator[Event]:
        """Yield the complete parent operation stream after a cursor."""
        async for event in self._task.operation.events(after_event_id=after_event_id):
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
            self._release_once()


    async def cancel(self) -> None:
        try:
            await self._task.cancel()
            try:
                await self._task.result()
            except Exception:
                pass
        finally:
            self._release_once()


    async def wait(self) -> OperationStatus:
        try:
            await self._task.result()
        except Exception:
            pass
        finally:
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
        self._turn_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._active_runs: set[SessionRun] = set()
        self._started = False
        self._closed = False


    @property
    def conversation_id(self) -> str:
        return self.conversation.conversation_id


    @property
    def is_started(self) -> bool:
        return self._started and not self._closed


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

            await self.conversation.start()
            await self.conversation.initialize_memory(
                self._llm,
                self._embed_model,
                token_limit=self._memory_token_limit,
                memory_top_k=self._memory_top_k,
            )
            self._started = True


    async def close(self) -> None:
        """Cancel active turns without closing shared manager resources."""
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._started = False
            active_runs = list(self._active_runs)

        await asyncio.gather(
            *(run.cancel() for run in active_runs),
            return_exceptions=True,
        )


    async def generate_response(
        self,
        user_query: str,
        *,
        retrieval_mode: RetrievalMode | str | None = None,
        operation: Operation | None = None,
    ) -> SessionRun:
        """Start one serialized autonomous turn in a new or supplied operation."""
        self._ensure_started()
        query = user_query.strip() if isinstance(user_query, str) else ""
        if not query:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "user_query must be a non-empty string.",
            )

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
                agent_run = await self.harness.start(
                    self.conversation,
                    query,
                    retrieval_mode=retrieval_mode,
                    operation=active_operation,
                )
                result = await agent_run.collect()
                await self.conversation.append_turn(agent_run.conversation_messages())
                generate_title = getattr(self.conversation, "generate_title", None)
                if generate_title is not None:
                    title_task = await generate_title(
                        self._llm,
                        query,
                        operation=active_operation,
                    )
                    await title_task.result()
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
            )
            session_run = SessionRun(task, self._release_run)
            run_holder["run"] = session_run
            self._active_runs.add(session_run)
            return session_run
        except Exception:
            self._turn_lock.release()
            raise


    def _release_run(self, run: SessionRun) -> None:
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
