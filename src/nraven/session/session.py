"""The user-facing connection between a conversation and the agent harness."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from ..agent.contracts import AgentRunResult, RetrievalPipelines
from ..agent.harness import AgentHarness
from ..agent.policy import AgentPolicy, RetrievalMode
from ..agent.prompts import render_manual_activity
from ..core.async_utils import await_completion
from ..core.config import RuntimeConfig, SQLITE_MAX_INTEGER
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
from ..data_management.knowledge_base import KnowledgeBase
from ..data_management.activity import ManualActivity, ManualActivityKind
from ..pipelines.reconstructor import Reconstructor
from ..pipelines.retrieval import _file_id_of



class SessionRetryInput(BaseModel):
    """Durable inputs required to retry one complete session turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conversation_id: str
    turn_id: UUID
    user_query: str
    retrieval_mode: RetrievalMode | None
    max_iterations: int
    runtime_config: RuntimeConfig
    memory_token_limit: int
    memory_top_k: int


    @field_validator("runtime_config", mode="before")
    @classmethod
    def _validate_runtime_config(cls, value: Any) -> RuntimeConfig:
        if isinstance(value, RuntimeConfig):
            return value
        try:
            return RuntimeConfig.from_dict(value)
        except RavenError as exc:
            raise ValueError(exc.message) from exc


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
    """Provide one temporary interaction medium for a conversation."""

    def __init__(
        self,
        conversation: Conversation,
        harness: AgentHarness | None,
        operation_manager: OperationManager,
        *,
        llm: Any,
        embed_model: Any,
        knowledge_base: KnowledgeBase | None = None,
        retrieval_pipelines: RetrievalPipelines | None = None,
        reconstructor: Reconstructor | None = None,
        get_runtime_config: Callable[[], RuntimeConfig] = RuntimeConfig,
        memory_token_limit: int = 4000,
        memory_top_k: int = 5,
    ) -> None:
        if harness is not None and llm is None:
            raise ValueError("llm is required")
        if harness is not None and embed_model is None:
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
        self._knowledge_base = knowledge_base
        self._retrieval_pipelines = retrieval_pipelines
        self._reconstructor = reconstructor
        self._get_runtime_config = get_runtime_config
        self._memory_token_limit = memory_token_limit
        self._memory_top_k = memory_top_k
        self._lifecycle_lock = asyncio.Lock()
        self._active_runs: set[SessionRun] = set()
        self._manual_tasks: set[OperationTask] = set()
        self._run_monitors: set[asyncio.Task[None]] = set()
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


    @property
    def has_active_runs(self) -> bool:
        return bool(self._active_runs or self._manual_tasks)


    async def start(self) -> None:
        """Open the persistent conversation used by this session."""
        async with self._lifecycle_lock:
            if self.is_started:
                return
            if self._closed:
                raise RavenError(
                    ErrorCode.CONVERSATION_CLOSED,
                    f"Session for conversation '{self.conversation_id}' is closed.",
                )

            await self.conversation.start()
            self._started = True


    async def close(self) -> None:
        """Cancel active work without closing shared manager resources."""
        async with self._lifecycle_lock:
            if self._cleanup_complete:
                return
            if self._close_task is None:
                self._closed = True
                self._started = False
                active_runs = list(self._active_runs)
                manual_tasks = tuple(self._manual_tasks)
                run_monitors = tuple(self._run_monitors)
                self._close_task = asyncio.create_task(
                    self._finish_close(active_runs, manual_tasks, run_monitors),
                    name=f"nraven-session-close-{self.conversation_id}",
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


    async def _finish_close(
        self,
        active_runs: list[SessionRun],
        manual_tasks: tuple[OperationTask, ...],
        run_monitors: tuple[asyncio.Task[None], ...],
    ) -> None:
        """Cancel only the work created through this session."""
        await asyncio.gather(
            *(run.cancel() for run in active_runs),
            *(task.cancel() for task in manual_tasks),
            return_exceptions=True,
        )
        await asyncio.gather(*run_monitors, return_exceptions=True)


    async def list_manual_activity(
        self, *, after_sequence: int = 0, limit: int = 50, turn_id: UUID | str | None = None,
    ) -> list[ManualActivity]:
        """Read saved manual actions without recording another entry."""
        self._ensure_started()
        return await self.conversation.list_manual_activity(
            after_sequence=after_sequence, limit=limit, turn_id=turn_id,
        )

    async def remove_from_activity(self, activity_id: UUID | str) -> bool:
        """Remove an activity that is still pending and outside the current turn."""
        self._ensure_started()
        return await self.conversation.remove_from_activity(activity_id)


    async def list_knowledges(self, *, operation: Operation | None = None) -> OperationTask:
        """List knowledges and record this browsing action."""
        knowledge_base = self._manual_knowledge_base()

        async def browse(_active: Operation) -> tuple[Any, dict[str, Any]]:
            knowledges = await knowledge_base.list()
            return knowledges, {
                "knowledges": [item["safe_name"] for item in knowledges],
            }

        return await self._run_manual_action(
            "list_knowledges", OperationType.SESSION_LIST_KNOWLEDGES, browse, operation,
        )


    async def list_files(
        self, knowledge_name: str, *, operation: Operation | None = None,
    ) -> OperationTask:
        """List files in a knowledge and record their references."""
        knowledge_base = self._manual_knowledge_base()

        async def browse(_active: Operation) -> tuple[Any, dict[str, Any]]:
            knowledge = knowledge_base.get(knowledge_name)
            files = await asyncio.to_thread(knowledge.list_files)
            return files, {
                "knowledge_name": knowledge.name,
                "files": [
                    {"knowledge_name": knowledge.name, "file_name": file["file_name"], "file_id": file["file_id"]}
                    for file in files
                ],
            }

        return await self._run_manual_action(
            "list_files", OperationType.SESSION_LIST_FILES, browse, operation,
        )


    async def list_sections(
        self,
        knowledge_name: str,
        file_id: str,
        *,
        limit: int = 20,
        after_section_index: int = 0,
        get_content: bool = False,
        operation: Operation | None = None,
    ) -> OperationTask:
        """List one page of a file's sections and record their references."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise RavenError(ErrorCode.INVALID_METADATA, "Section limit must be between 1 and 50.")
        if type(after_section_index) is not int or not 0 <= after_section_index <= SQLITE_MAX_INTEGER:
            raise RavenError(ErrorCode.INVALID_METADATA, "Invalid section cursor.")
        if type(get_content) is not bool:
            raise RavenError(ErrorCode.INVALID_METADATA, "get_content must be a boolean.")
        knowledge_base = self._manual_knowledge_base()

        async def browse(_active: Operation) -> tuple[Any, dict[str, Any]]:
            knowledge = knowledge_base.get(knowledge_name)
            file = await asyncio.to_thread(knowledge.get_file, file_id)
            if file is None:
                raise RavenError(ErrorCode.FILE_NOT_FOUND, f"File '{file_id}' does not exist in '{knowledge.name}'.")
            sections = await asyncio.to_thread(
                knowledge.list_sections_page_by_file_id, file_id, limit, after_section_index,
            )
            reference = {
                "knowledge_name": knowledge.name, "file_name": file["file_name"], "file_id": file_id,
            }
            details = {
                "file": reference,
                "sections": [reference | {"section_id": item["section_id"]} for item in sections],
                "after_section_index": after_section_index,
                "limit": limit,
                "get_content": get_content,
            }
            if not get_content:
                for section in sections:
                    section.pop("raw_content", None)
            return sections, details

        return await self._run_manual_action(
            "list_sections", OperationType.SESSION_LIST_SECTIONS, browse, operation,
        )


    async def retrieve(
        self,
        query: str,
        *,
        retrieval_mode: RetrievalMode | str,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Search the conversation scope and record the ordered results."""
        self._ensure_started()
        mode = AgentPolicy.parse_mode(retrieval_mode)
        if mode is None:
            raise RavenError(ErrorCode.INVALID_RETRIEVAL_MODE, "Choose a manual retrieval strategy.")
        query = query.strip() if isinstance(query, str) else ""
        if not query:
            raise RavenError(ErrorCode.INVALID_METADATA, "Retrieval query must be a non-empty string.")
        if self._retrieval_pipelines is None:
            if self._llm is None:
                raise RavenError(ErrorCode.LLM_MODEL_REQUIRED, "Configure models before using retrieval.")
            if self._embed_model is None:
                raise RavenError(ErrorCode.EMBEDDING_MODEL_REQUIRED, "Configure models before using retrieval.")
            raise RavenError(ErrorCode.INVALID_METADATA, "Session retrieval pipelines are unavailable.")
        pipeline = getattr(self._retrieval_pipelines, mode.value)
        scope = self.conversation.scope
        options = RetrievalPipelines.options(mode, self._get_runtime_config())

        async def search(active: Operation) -> tuple[Any, dict[str, Any]]:
            task = await pipeline.retrieve(query, scope=scope, operation=active, **options)
            result = await _manual_child_result(task)
            details = {
                "query": query,
                "strategy": mode.value,
                "scope": scope.model_dump(mode="json"),
                "settings": options,
                "sources": _compact_retrieval_sources(result),
            }
            if mode is RetrievalMode.AGREEMENT:
                details["agreement_type"] = result["agreement_type"]
            return result, details

        return await self._run_manual_action(
            "retrieval", OperationType.SESSION_RETRIEVE, search, operation,
        )


    async def open_file(
        self,
        knowledge_name: str,
        file_id: str,
        *,
        origin_activity_id: UUID | None = None,
        origin_turn_id: UUID | None = None,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Open a stored file record and record the deliberate inspection."""
        return await self._open_source(
            knowledge_name, file_id, "open_file", OperationType.SESSION_OPEN_FILE,
            origin_activity_id, origin_turn_id, operation,
        )


    async def open_section(
        self,
        knowledge_name: str,
        section_id: str,
        *,
        origin_activity_id: UUID | None = None,
        origin_turn_id: UUID | None = None,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Open a stored section and record the deliberate inspection."""
        return await self._open_source(
            knowledge_name, section_id, "open_section", OperationType.SESSION_OPEN_SECTION,
            origin_activity_id, origin_turn_id, operation,
        )


    async def _open_source(
        self,
        knowledge_name: str,
        source_id: str,
        kind: ManualActivityKind,
        operation_type: OperationType,
        origin_activity_id: UUID | None,
        origin_turn_id: UUID | None,
        operation: Operation | None,
    ) -> OperationTask:
        knowledge_base = self._manual_knowledge_base()

        async def inspect_source(_active: Operation) -> tuple[Any, dict[str, Any]]:
            knowledge = knowledge_base.get(knowledge_name)
            is_file = kind == "open_file"
            lookup = knowledge.get_file if is_file else knowledge.get_section
            error_code = ErrorCode.FILE_NOT_FOUND if is_file else ErrorCode.SECTION_NOT_FOUND
            result = await asyncio.to_thread(lookup, source_id)
            if result is None:
                raise RavenError(error_code, f"Source '{source_id}' does not exist in '{knowledge.name}'.")
            reference = {
                "knowledge_name": knowledge.name,
                "file_id": result["file_id"],
                "file_name": result["file_name"],
            }
            if not is_file:
                reference.update(section_id=result["section_id"], source_range=result["source_range"])
            details: dict[str, Any] = {"source": reference}
            if origin_activity_id is not None:
                details["origin_activity_id"] = str(origin_activity_id)
            if origin_turn_id is not None:
                details["origin_turn_id"] = str(origin_turn_id)
            return result, details

        return await self._run_manual_action(kind, operation_type, inspect_source, operation)


    async def reconstruct(
        self,
        retrieval_result: list[dict[str, Any]] | dict[str, Any],
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Reconstruct selected sources and record the input and output references."""
        self._ensure_started()
        reconstructor = self._reconstructor
        if reconstructor is None:
            raise RavenError(ErrorCode.INVALID_METADATA, "Session reconstruction is unavailable.")
        sections = Reconstructor._normalize_retrieval_result(retrieval_result)
        references = [_section_reference(section) for section in sections]

        async def reconstruct_sources(active: Operation) -> tuple[Any, dict[str, Any]]:
            task = await reconstructor.reconstruct(sections, operation=active)
            files = await _manual_child_result(task)
            file_ids = {(ref["knowledge_name"], ref["file_name"]): ref["file_id"] for ref in references}
            return files, {
                "inputs": references,
                "files": [
                    {
                        "knowledge_name": file["knowledge_name"],
                        "file_name": file["file_name"],
                        "file_id": file_ids[(file["knowledge_name"], file["file_name"])],
                    }
                    for file in files
                ],
            }

        return await self._run_manual_action(
            "reconstruction", OperationType.SESSION_RECONSTRUCT, reconstruct_sources, operation,
        )


    def _manual_knowledge_base(self) -> KnowledgeBase:
        if self._knowledge_base is None:
            raise RavenError(ErrorCode.INVALID_METADATA, "Session source access is unavailable.")
        return self._knowledge_base


    async def _run_manual_action(
        self,
        kind: ManualActivityKind,
        operation_type: OperationType,
        action: Callable[[Operation], Awaitable[tuple[Any, dict[str, Any]]]],
        operation: Operation | None,
    ) -> OperationTask:
        started_at = datetime.now(timezone.utc)

        async def worker(active: Operation) -> Any:
            async def run_and_record() -> Any:
                result, details = await action(active)
                active.raise_if_cancelled()
                entry = await self.conversation._record_manual_activity(
                    kind, operation_id=active.operation_id, started_at=started_at, details=details,
                )
                data: dict[str, Any] = {
                    "conversation_id": self.conversation_id,
                    "activity_id": str(entry.activity_id),
                    "sequence": entry.sequence,
                    "kind": entry.kind,
                }
                # The reconstructor already streams each reconstructed file.
                if kind != "reconstruction":
                    data["result"] = result
                await active.publish(Event(
                    type=EventType.MANUAL_ACTIVITY_RECORDED,
                    data=data,
                ))
                return result

            return await self.conversation._run_in_use(run_and_record)

        async with self._lifecycle_lock:
            self._ensure_started()
            active = operation or await self._operation_manager.create(operation_type)
            task = await active.run(operation_type, worker)
            self._manual_tasks.add(task)
            monitor = asyncio.create_task(self._observe_manual_task(task))
            self._run_monitors.add(monitor)
            monitor.add_done_callback(self._run_monitors.discard)
            return task


    async def _observe_manual_task(self, task: OperationTask) -> None:
        try:
            await task.result()
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            self._manual_tasks.discard(task)


    async def generate_response(
        self,
        user_query: str,
        *,
        retrieval_mode: RetrievalMode | str | None = None,
        operation: Operation | None = None,
        retry_of: OperationTaskRecord | None = None,
    ) -> SessionRun:
        """Start a turn with current retrieval defaults or its saved retry snapshot."""
        self._ensure_started()
        harness = self.harness
        if harness is None:
            raise RavenError(ErrorCode.LLM_MODEL_REQUIRED, "Agent turns require a session with an agent harness.")
        if retry_of is None:
            query = user_query.strip() if isinstance(user_query, str) else ""
            if not query:
                raise RavenError(
                    ErrorCode.INVALID_METADATA,
                    "user_query must be a non-empty string.",
                )
            selected_mode = AgentPolicy.parse_mode(retrieval_mode)
            retry_input = SessionRetryInput(
                conversation_id=self.conversation_id,
                turn_id=uuid4(),
                user_query=query,
                retrieval_mode=selected_mode,
                max_iterations=harness.max_iterations,
                runtime_config=self._get_runtime_config(),
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

        turn_claim_id = uuid4()
        await self.conversation._claim_turn(turn_claim_id)
        turn_claimed = True

        async def execute_turn(active_operation: Operation) -> AgentRunResult:
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
                activity_message = await self.conversation._capture_activity(
                    lambda activities: render_manual_activity(
                        activities, query=query, token_budget=min(512, self._memory_token_limit // 8),
                    ),
                    limit=20,
                )
                agent_run = await harness.start(
                    self.conversation,
                    query,
                    retrieval_mode=selected_mode,
                    runtime_config=retry_input.runtime_config,
                    activity_message=activity_message,
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

        async def worker(active_operation: Operation) -> AgentRunResult:
            try:
                return await execute_turn(active_operation)
            finally:
                await self._release_turn_claim(turn_claim_id, propagate_cancellation=False)

        try:
            await self.conversation.initialize_memory(
                self._llm,
                self._embed_model,
                token_limit=self._memory_token_limit,
                memory_top_k=self._memory_top_k,
            )
            async with self._lifecycle_lock:
                self._ensure_started()
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
                self._active_runs.add(session_run)
                monitor = asyncio.create_task(
                    self._observe_run(session_run, turn_claim_id),
                    name=f"nraven-session-run-{turn_id}",
                )
                self._run_monitors.add(monitor)
                monitor.add_done_callback(self._run_monitors.discard)
                return session_run
        except BaseException:
            if turn_claimed:
                await self._release_turn_claim(turn_claim_id, propagate_cancellation=False)
            raise


    def _release_run(self, run: SessionRun) -> None:
        if run not in self._active_runs:
            return
        self._active_runs.discard(run)


    async def _observe_run(self, run: SessionRun, turn_claim_id: UUID) -> None:
        try:
            await run.task.result()
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            try:
                await self._release_turn_claim(turn_claim_id, propagate_cancellation=False)
            finally:
                run._release_once()


    async def _release_turn_claim(
        self,
        turn_claim_id: UUID,
        *,
        propagate_cancellation: bool = True,
    ) -> None:
        release = asyncio.create_task(
            self.conversation._release_turn(turn_claim_id)
        )
        _, cancellation_requested = await await_completion(release)
        if cancellation_requested and propagate_cancellation:
            raise asyncio.CancelledError


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


def _section_reference(section: dict[str, Any]) -> dict[str, str]:
    section_id = section["section_id"]
    return {
        "knowledge_name": section["knowledge_name"],
        "file_name": section["file_name"],
        "file_id": _file_id_of(section_id),
        "section_id": section_id,
    }


async def _manual_child_result(task: OperationTask) -> Any:
    try:
        return await task.result()
    except asyncio.CancelledError:
        await await_completion(asyncio.create_task(task.cancel()))
        raise


def _compact_retrieval_sources(
    result: list[dict[str, Any]] | dict[str, Any],
) -> list[dict[str, str]] | dict[str, list[dict[str, str]]]:
    content = result["retrieved_content"] if isinstance(result, dict) else result
    if isinstance(content, dict):
        return {branch: [_section_reference(section) for section in hits] for branch, hits in content.items()}
    return [_section_reference(section) for section in content]
