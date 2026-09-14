"""Operation lifecycle and task execution for Raven."""

from __future__ import annotations

import asyncio
import copy
import json
import math
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .async_utils import await_completion
from .config import (
    DEFAULT_EVENT_REPLAY_PAGE_SIZE,
    DEFAULT_FINISHED_OPERATION_CACHE_SIZE,
    DEFAULT_OPERATION_CLEANUP_BATCH_SIZE,
    DEFAULT_OPERATION_PAGE_SIZE,
    OPERATION_SYNC_INTERVAL_SECONDS,
    PathConfig,
)
from .errors import ErrorCode, RavenError, error_payload
from .events import (
    Event,
    EventStream,
    EventType,
)
from .operation_store import SQLiteOperationStore



class OperationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"



class OperationType(StrEnum):
    """Stable names for operations provided by Raven's built-in components."""

    RAVEN_LOAD_MODELS = "nraven.load_models"
    RUNTIME_SETTINGS_UPDATE = "runtime.settings.update"
    RUNTIME_SETTINGS_RESET = "runtime.settings.reset"

    MODEL_CHECK_CONNECTION = "model.check_connection"
    MODEL_LIST = "model.list"
    MODEL_INSPECT = "model.inspect"
    MODEL_PULL = "model.pull"
    MODEL_DELETE = "model.delete"
    MODEL_LOAD_LLM = "model.load_llm"
    MODEL_LOAD_EMBEDDING = "model.load_embedding"
    MODEL_UNLOAD_LLM = "model.unload_llm"
    MODEL_UNLOAD_EMBEDDING = "model.unload_embedding"

    KNOWLEDGE_SET_SUMMARY = "knowledge.set_summary"
    KNOWLEDGE_INGEST = "knowledge.ingest"
    KNOWLEDGE_DELETE_FILE = "knowledge.delete_file"
    KNOWLEDGE_CREATE = "knowledge.create"
    KNOWLEDGE_DELETE = "knowledge.delete"

    INGESTION_RUN = "ingestion.run"
    INGESTION_CLEANUP = "ingestion.cleanup"

    RETRIEVAL_EMBEDDED_LOCAL = "retrieval.embedded.local"
    RETRIEVAL_EMBEDDED_GLOBAL = "retrieval.embedded.global"
    RETRIEVAL_HIERARCHICAL_LOCAL = "retrieval.hierarchical.local"
    RETRIEVAL_HIERARCHICAL_GLOBAL = "retrieval.hierarchical.global"
    RETRIEVAL_HIERARCHICAL_BY_KNOWLEDGE = "retrieval.hierarchical.by_knowledge"
    RETRIEVAL_HIERARCHICAL_BY_FILE = "retrieval.hierarchical.by_file"
    RETRIEVAL_AGREEMENT_LOCAL = "retrieval.agreement.local"
    RETRIEVAL_AGREEMENT_GLOBAL = "retrieval.agreement.global"
    RETRIEVAL_VECTOR_CONDITIONED_LOCAL = "retrieval.vector_conditioned.local"
    RETRIEVAL_VECTOR_CONDITIONED_GLOBAL = "retrieval.vector_conditioned.global"

    RECONSTRUCTION_RECONSTRUCT = "reconstruction.reconstruct"
    RECONSTRUCTION_FROM_TURN = "reconstruction.from_turn"

    CONVERSATION_SAVE_PREFERENCE = "conversation.save_preference"
    CONVERSATION_REMOVE_PREFERENCE = "conversation.remove_preference"
    CONVERSATION_UPDATE = "conversation.update"
    CONVERSATION_GENERATE_TITLE = "conversation.generate_title"
    CONVERSATION_GET_CONTEXT = "conversation.get_context"
    CONVERSATION_APPEND_TURN = "conversation.append_turn"
    CONVERSATION_RECONCILE_TURN = "conversation.reconcile_turn"
    CONVERSATION_CREATE = "conversation.create"
    CONVERSATION_UPDATE_METADATA = "conversation.update_metadata"
    CONVERSATION_DELETE = "conversation.delete"

    CHAT_GENERATE_RESPONSE = "chat.generate_response"
    SESSION_GENERATE_RESPONSE = "session.generate_response"



class RetryPolicy(StrEnum):
    """User-visible retry behavior allowed for an operation task."""

    NEVER = "never"
    USER_CONFIRMED = "user_confirmed"



@dataclass(frozen=True, slots=True)
class RetryRule:
    """Static retry policy for one operation task type."""

    policy: RetryPolicy
    max_attempts: int
    retryable_error_codes: frozenset[str]




_COMMON_RETRYABLE_ERRORS = frozenset(
    {
        ErrorCode.OPERATION_INTERRUPTED.value,
        ErrorCode.INTERNAL_ERROR.value,
        ErrorCode.OPERATION_SYNC_FAILED.value,
        ErrorCode.OPERATION_DATABASE_FAILED.value,
        ErrorCode.MODEL_PROVIDER_FAILED.value,
        ErrorCode.OLLAMA_UNAVAILABLE.value,
        ErrorCode.OLLAMA_OPERATION_FAILED.value,
    }
)

_RETRY_RULES = {
    OperationType.INGESTION_RUN.value: RetryRule(
        policy=RetryPolicy.USER_CONFIRMED,
        max_attempts=3,
        retryable_error_codes=_COMMON_RETRYABLE_ERRORS
        | {
            ErrorCode.SOURCE_FILE_NOT_FOUND.value,
            ErrorCode.SOURCE_FILE_CHANGED.value,
            ErrorCode.SOURCE_FILE_UNREADABLE.value,
            ErrorCode.DOCUMENT_PARSE_FAILED.value,
            ErrorCode.NO_SECTIONS_PRODUCED.value,
            ErrorCode.SECTION_METADATA_EXTRACTION_FAILED.value,
            ErrorCode.INVALID_EMBEDDING_RESULT.value,
            ErrorCode.EMBEDDING_DIMENSION_MISMATCH.value,
            ErrorCode.INGESTION_RECONCILIATION_FAILED.value,
            ErrorCode.PERSISTENCE_FAILED.value,
        },
    ),
    OperationType.RECONSTRUCTION_RECONSTRUCT.value: RetryRule(
        policy=RetryPolicy.USER_CONFIRMED,
        max_attempts=3,
        retryable_error_codes=_COMMON_RETRYABLE_ERRORS
        | {
            ErrorCode.KNOWLEDGE_NOT_FOUND.value,
            ErrorCode.FILE_NOT_FOUND.value,
            ErrorCode.SECTION_NOT_FOUND.value,
        },
    ),
    OperationType.SESSION_GENERATE_RESPONSE.value: RetryRule(
        policy=RetryPolicy.USER_CONFIRMED,
        max_attempts=3,
        retryable_error_codes=_COMMON_RETRYABLE_ERRORS
        | {
            ErrorCode.PERSISTENCE_FAILED.value,
        },
    ),
}

_NO_RETRY_RULE = RetryRule(
    policy=RetryPolicy.NEVER,
    max_attempts=1,
    retryable_error_codes=frozenset(),
)


def _retry_rule(name: str) -> RetryRule:
    return _RETRY_RULES.get(name, _NO_RETRY_RULE)




class OperationTaskRecord(BaseModel):
    """Durable task state used to evaluate a user-confirmed retry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    operation_id: UUID
    name: str
    is_root: bool
    status: OperationStatus
    retry_policy: RetryPolicy
    retry_input: dict[str, Any] | None
    attempt: int
    retry_of_operation_id: UUID | None
    retry_of_task_id: UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None


    @property
    def max_attempts(self) -> int:
        return _retry_rule(self.name).max_attempts


    @property
    def attempts_remaining(self) -> int:
        return max(self.max_attempts - self.attempt, 0)


    @property
    def can_retry(self) -> bool:
        error_code = self.error.get("code") if self.error is not None else None
        rule = _retry_rule(self.name)
        return (
            self.retry_policy == RetryPolicy.USER_CONFIRMED
            and self.retry_input is not None
            and self.status == OperationStatus.FAILED
            and self.attempt < rule.max_attempts
            and error_code in rule.retryable_error_codes
        )



class OperationRecord(BaseModel):
    """Durable operation state suitable for history and status discovery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: UUID
    name: str
    status: OperationStatus
    last_event_id: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None


    @property
    def is_finished(self) -> bool:
        return self.status in _TERMINAL_STATUSES



class OperationCleanupResult(BaseModel):
    """Outcome of one bounded expired-operation cleanup pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    deleted_operation_ids: tuple[str, ...] = ()
    skipped_operation_ids: tuple[str, ...] = ()
    failures: dict[str, dict[str, Any]] = Field(default_factory=dict)


OperationWorker = Callable[["Operation"], Awaitable[Any]]
TaskWorker = Callable[["Operation"], Awaitable[Any]]
_TERMINAL_STATUSES = frozenset(
    {
        OperationStatus.COMPLETED,
        OperationStatus.FAILED,
        OperationStatus.CANCELLED,
    }
)
_TERMINAL_TASK_EVENT_TYPES = frozenset(
    {
        EventType.OPERATION_TASK_COMPLETED,
        EventType.OPERATION_TASK_FAILED,
        EventType.OPERATION_TASK_CANCELLED,
    }
)
_CURRENT_TASK: ContextVar[tuple[UUID, str] | None] = ContextVar(
    "nraven_current_operation_task",
    default=None,
)



class Operation:
    """One root operation, its event stream, and its child task registry."""

    def __init__(
        self,
        operation_id: UUID,
        name: str,
        stream: EventStream,
        store: SQLiteOperationStore,
        *,
        created_at: datetime | None = None,
        on_finished: Callable[["Operation"], None] | None = None,
    ) -> None:
        self.operation_id = operation_id
        self.name = name
        self._stream = stream
        self._store = store
        self._created_at = created_at or datetime.now(timezone.utc)
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._status = OperationStatus.QUEUED
        self._result: Any = None
        self._error: dict[str, Any] | None = None
        self._cancellation_requested = False
        self._task: asyncio.Task[Any] | None = None
        self._child_tasks: dict[UUID, OperationTask] = {}
        self._terminal_exception: BaseException | None = None
        self._restored = False
        self._on_finished = on_finished
        self._lock = asyncio.Lock()


    @property
    def status(self) -> OperationStatus:
        return self._status


    @property
    def created_at(self) -> datetime:
        return self._created_at


    @property
    def started_at(self) -> datetime | None:
        return self._started_at


    @property
    def finished_at(self) -> datetime | None:
        return self._finished_at


    @property
    def result(self) -> Any:
        return self._result


    @property
    def error(self) -> dict[str, Any] | None:
        return self._error


    @property
    def cancellation_requested(self) -> bool:
        return self._cancellation_requested


    @property
    def is_finished(self) -> bool:
        return self._status in _TERMINAL_STATUSES


    @property
    def tasks(self) -> tuple["OperationTask", ...]:
        return tuple(self._child_tasks.values())


    async def get_task(self, task_id: UUID | str) -> OperationTaskRecord:
        """Load one durable task record owned by this operation."""
        parsed_id = self._parse_task_id(task_id)
        record = await self._store.read_task(str(parsed_id))
        if record is None or str(record.get("operation_id")) != str(self.operation_id):
            raise RavenError(
                ErrorCode.OPERATION_TASK_NOT_FOUND,
                f"Operation task '{parsed_id}' was not found in operation "
                f"'{self.operation_id}'.",
            )
        return OperationTaskRecord.model_validate(record)


    async def get_retry(
        self,
        task_id: UUID | str,
    ) -> OperationTaskRecord | None:
        """Return the direct retry of one task owned by this operation."""
        task = await self.get_task(task_id)
        record = await self._store.read_retry(str(task.task_id))
        if record is None:
            return None
        return OperationTaskRecord.model_validate(record)


    async def list_tasks(
        self,
        *,
        status: OperationStatus | str | None = None,
        limit: int = DEFAULT_OPERATION_PAGE_SIZE,
        after_task_id: UUID | str | None = None,
    ) -> list[OperationTaskRecord]:
        """List this operation's durable tasks in newest-first order."""
        parsed_status = self._parse_status(status)
        parsed_cursor: UUID | None = None
        if after_task_id is not None:
            parsed_cursor = self._parse_task_id(after_task_id)
            cursor = await self._store.read_task(str(parsed_cursor))
            if (
                cursor is None
                or str(cursor.get("operation_id")) != str(self.operation_id)
            ):
                raise RavenError(
                    ErrorCode.OPERATION_TASK_NOT_FOUND,
                    f"Operation task '{parsed_cursor}' was not found in operation "
                    f"'{self.operation_id}'.",
                )
        self._validate_page_size(limit)
        records = await self._store.list_tasks(
            operation_id=str(self.operation_id),
            status=parsed_status.value if parsed_status is not None else None,
            after_task_id=str(parsed_cursor) if parsed_cursor is not None else None,
            limit=limit,
        )
        return [OperationTaskRecord.model_validate(record) for record in records]


    async def run(
        self,
        name: str,
        worker: TaskWorker,
        *,
        retry_input: dict[str, Any] | None = None,
        retry_of: OperationTaskRecord | None = None,
    ) -> "OperationTask":
        """Run the root task or a child task within this operation."""
        retry_policy = _retry_rule(str(name)).policy
        normalized_retry_input = self._normalize_retry_input(
            retry_input,
            retry_policy,
        )
        if retry_of is not None:
            if not retry_of.can_retry or retry_of.name != str(name):
                raise RavenError(
                    ErrorCode.OPERATION_TASK_NOT_RETRYABLE,
                    f"Operation task '{retry_of.task_id}' cannot be retried as '{name}'.",
                )
            attempt = retry_of.attempt + 1
            retry_of_operation_id = retry_of.operation_id
            retry_of_task_id = retry_of.task_id
        else:
            attempt = 1
            retry_of_operation_id = None
            retry_of_task_id = None

        async with self._lock:
            if self.is_finished:
                raise RavenError(
                    ErrorCode.OPERATION_FINISHED,
                    f"Operation '{self.operation_id}' is already finished.",
                )
            if self._cancellation_requested:
                raise asyncio.CancelledError

            is_root = self._task is None
            if is_root:
                if name != self.name:
                    raise RavenError(
                        ErrorCode.INVALID_OPERATION_NAME,
                        "The root task name must match the operation name.",
                        details={
                            "operation_name": self.name,
                            "task_name": name,
                        },
                    )

                task = OperationTask(
                    self,
                    name,
                    worker,
                    root=True,
                    parent_task_id=None,
                    retry_policy=retry_policy,
                    retry_input=normalized_retry_input,
                    attempt=attempt,
                    retry_of_operation_id=retry_of_operation_id,
                    retry_of_task_id=retry_of_task_id,
                )
                registration_cancelled = await task._persist()
                self._register_task(task)
                if registration_cancelled:
                    self._cancellation_requested = True
                else:
                    self._task = asyncio.create_task(
                        self._run(task._run_root),
                        name=f"nraven-operation-{self.operation_id}",
                    )
                    return task

            else:
                if self._status != OperationStatus.RUNNING:
                    raise RavenError(
                        ErrorCode.OPERATION_FINISHED,
                        f"Operation '{self.operation_id}' is not accepting child tasks.",
                    )

                task = OperationTask(
                    self,
                    name,
                    worker,
                    root=False,
                    parent_task_id=(
                        _CURRENT_TASK.get()[0]
                        if _CURRENT_TASK.get() is not None
                        else None
                    ),
                    retry_policy=retry_policy,
                    retry_input=normalized_retry_input,
                    attempt=attempt,
                    retry_of_operation_id=retry_of_operation_id,
                    retry_of_task_id=retry_of_task_id,
                )
                registration_cancelled = await task._persist()
                self._register_task(task)
                if not registration_cancelled:
                    await task.start()
                    return task

        await task._finalize_cancelled()
        if is_root:
            finalization = asyncio.create_task(self._finish_cancelled())
            await await_completion(finalization)
        raise asyncio.CancelledError


    @staticmethod
    def _normalize_retry_input(
        retry_input: dict[str, Any] | None,
        retry_policy: RetryPolicy,
    ) -> dict[str, Any] | None:
        if retry_input is None:
            return None
        if retry_policy == RetryPolicy.NEVER:
            raise RavenError(
                ErrorCode.OPERATION_TASK_NOT_RETRYABLE,
                "This operation task type does not permit retry input.",
            )
        try:
            return json.loads(json.dumps(retry_input, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise RavenError(
                ErrorCode.INVALID_RETRY_INPUT,
                "Retry input must be JSON serializable.",
            ) from exc


    async def wait(self) -> "Operation":
        """Wait for the root worker and return this operation."""
        task = self._task
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
        return self


    async def cancel(self) -> "Operation":
        """Cancel the root worker and all active child tasks."""
        async with self._lock:
            if self.is_finished:
                return self
            self._cancellation_requested = True
            root_task = self._task
            child_tasks = tuple(
                task for task in self._child_tasks.values() if not task._root
            )

        for child_task in child_tasks:
            await child_task.cancel()
        await asyncio.gather(
            *(
                child_task._task
                for child_task in child_tasks
                if child_task._task is not None
            ),
            return_exceptions=True,
        )
        if root_task is not None and not root_task.done():
            root_task.cancel()
            await asyncio.gather(root_task, return_exceptions=True)

        root_handle = next(
            (task for task in self._child_tasks.values() if task.is_root),
            None,
        )
        if root_handle is not None and not root_handle.is_finished:
            await root_handle._finalize_cancelled()
        if not self.is_finished:
            await self._finish_cancelled()
        return self


    async def publish(self, event: Event) -> Event:
        """Publish a domain event and attach the active task correlation."""
        if self.is_finished:
            raise RuntimeError(f"operation '{self.operation_id}' is already finished")
        if event.is_final:
            raise ValueError("workers cannot publish final operation events")
        if event.operation_id is not None and event.operation_id != self.operation_id:
            raise ValueError("event operation_id does not match the operation")

        current_task = _CURRENT_TASK.get()
        updates: dict[str, Any] = {"operation_id": self.operation_id}
        if current_task is not None:
            updates["task_id"] = event.task_id or current_task[0]
            updates["task_name"] = event.task_name or current_task[1]

        return await self._stream.publish(event.model_copy(update=updates))


    def raise_if_cancelled(self) -> None:
        if self._cancellation_requested:
            raise asyncio.CancelledError


    async def events(self, after_event_id: int = 0) -> AsyncIterator[Event]:
        async for event in self._stream.events(after_event_id=after_event_id):
            yield event


    async def read_events(
        self,
        after_event_id: int = 0,
        *,
        limit: int | None = None,
    ) -> list[Event]:
        """Read a bounded event page without opening a live reader."""
        return await self._stream.read(after_event_id, limit=limit)


    async def _run(self, worker: OperationWorker) -> None:
        try:
            await self._mark_running()
            result = await worker(self)
            await self._join_children()
            if self._cancellation_requested:
                await self._finish_cancelled()
            else:
                await self._finish_completed(result)
        except asyncio.CancelledError as exc:
            await self._cancel_children()
            try:
                root_handle = next(
                    (task for task in self._child_tasks.values() if task.is_root),
                    None,
                )
                if root_handle is not None and not root_handle.is_finished:
                    await root_handle._finalize_cancelled(exc)
                await self._finish_cancelled()
            except BaseException as finish_error:
                self._set_local_terminal_failure(finish_error)
            self._terminal_exception = exc
        except Exception as exc:
            await self._cancel_children()
            try:
                await self._finish_failed(exc)
            except BaseException as finish_error:
                self._set_local_terminal_failure(finish_error)


    async def _join_children(self) -> None:
        """Wait for every child owned by this operation and surface failures."""
        while True:
            async with self._lock:
                children = tuple(
                    task
                    for task in self._child_tasks.values()
                    if not task.is_root and not task.is_finished
                )
            if not children:
                break
            await asyncio.gather(
                *(task._wait_for_worker() for task in children),
                return_exceptions=True,
            )

        failed = next(
            (
                task
                for task in self._child_tasks.values()
                if not task.is_root
                and not task.result_observed
                and task.status == OperationStatus.FAILED
            ),
            None,
        )
        if failed is not None:
            if failed.error is not None:
                raise failed.error
            raise RavenError(
                ErrorCode.INTERNAL_ERROR,
                f"Operation task '{failed.task_id}' failed.",
            )

        cancelled = next(
            (
                task
                for task in self._child_tasks.values()
                if not task.is_root
                and not task.result_observed
                and task.status == OperationStatus.CANCELLED
            ),
            None,
        )
        if cancelled is not None:
            raise asyncio.CancelledError


    async def _cancel_children(self) -> None:
        children = tuple(
            task for task in self._child_tasks.values() if not task.is_root
        )
        await asyncio.gather(
            *(task.cancel() for task in children if not task.is_finished),
            return_exceptions=True,
        )


    def _set_local_terminal_failure(self, error: BaseException) -> None:
        self._status = OperationStatus.FAILED
        self._finished_at = datetime.now(timezone.utc)
        self._result = None
        self._error = error_payload(error)
        self._terminal_exception = error


    async def _mark_running(self) -> None:
        async with self._lock:
            if self._cancellation_requested:
                raise asyncio.CancelledError
            self._status = OperationStatus.RUNNING
            self._started_at = datetime.now(timezone.utc)

        await self._stream.publish(
            Event(
                type=EventType.OPERATION_STARTED,
                data={"name": self.name},
            )
        )


    async def _persist_queued(self) -> None:
        await self._stream.publish(
            Event(
                type=EventType.OPERATION_QUEUED,
                data={"name": self.name},
            )
        )


    async def _finish_completed(self, result: Any) -> None:
        await self._finish(
            status=OperationStatus.COMPLETED,
            event_type=EventType.OPERATION_COMPLETED,
            result=result,
        )


    async def _finish_failed(self, error: BaseException) -> None:
        await self._finish(
            status=OperationStatus.FAILED,
            event_type=EventType.OPERATION_FAILED,
            error=error_payload(error),
        )
        self._terminal_exception = error


    async def _finish_cancelled(self) -> None:
        await self._finish(
            status=OperationStatus.CANCELLED,
            event_type=EventType.OPERATION_CANCELLED,
        )


    async def _recover_interrupted(self) -> bool:
        """Finalize a reconstructed non-terminal operation after a restart."""
        async with self._lock:
            if self.is_finished:
                return False
            if self._task is not None:
                raise RuntimeError("an active operation cannot be recovered as interrupted")
            previous_status = self._status

        interruption = RavenError(
            ErrorCode.OPERATION_INTERRUPTED,
            "The operation was interrupted by a previous process termination.",
        )
        await self._finish(
            status=OperationStatus.FAILED,
            event_type=EventType.OPERATION_FAILED,
            error=interruption.as_payload(),
            event_data={
                "previous_status": previous_status.value,
                "recovered_after_restart": True,
            },
        )
        return True


    async def _recover_interrupted_tasks(self) -> None:
        """Finalize persisted non-terminal tasks owned by this operation."""
        records = await self._store.unfinished_tasks(str(self.operation_id))
        interruption = RavenError(
            ErrorCode.OPERATION_INTERRUPTED,
            "The operation task was interrupted by a previous process termination.",
        )
        for record in records:
            await self.publish(
                Event(
                    type=EventType.OPERATION_TASK_FAILED,
                    task_id=UUID(str(record["task_id"])),
                    task_name=str(record["name"]),
                    data={
                        "name": str(record["name"]),
                        "error": interruption.as_payload(),
                        "previous_status": str(record["status"]),
                        "recovered_after_restart": True,
                    },
                )
            )


    async def _finish(
        self,
        *,
        status: OperationStatus,
        event_type: EventType,
        result: Any = None,
        error: dict[str, Any] | None = None,
        event_data: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            if self.is_finished:
                return
            data: dict[str, Any] = {"name": self.name, **(event_data or {})}
            if error is not None:
                data["error"] = error
            persisted = await self._stream.publish(
                Event(
                    type=event_type,
                    data=data,
                    is_final=True,
                )
            )
            self._status = status
            self._finished_at = persisted.timestamp
            self._result = result
            self._error = error
        if self._on_finished is not None:
            self._on_finished(self)


    @classmethod
    def from_events(
        cls,
        operation_id: UUID,
        stream: EventStream,
        store: SQLiteOperationStore,
        events: list[Event],
    ) -> "Operation":
        """Reconstruct a root operation from its persisted event history."""
        if not events:
            raise RavenError(
                ErrorCode.OPERATION_NOT_FOUND,
                f"Operation '{operation_id}' has no persisted events.",
            )

        queued = next(
            (event for event in events if event.type == EventType.OPERATION_QUEUED),
            None,
        )
        name = queued.data.get("name") if queued else None
        if not isinstance(name, str) or not name:
            name = "unknown"

        operation = cls(
            operation_id=operation_id,
            name=name,
            stream=stream,
            store=store,
            created_at=events[0].timestamp,
        )
        operation._restore(events)
        operation._restored = True
        return operation


    @classmethod
    def from_record(
        cls,
        record: OperationRecord,
        stream: EventStream,
        store: SQLiteOperationStore,
        *,
        on_finished: Callable[["Operation"], None] | None = None,
    ) -> "Operation":
        """Reconstruct operation status without replaying its domain events."""
        operation = cls(
            operation_id=record.operation_id,
            name=record.name,
            stream=stream,
            store=store,
            created_at=record.created_at,
            on_finished=on_finished,
        )
        operation._status = record.status
        operation._started_at = record.started_at
        operation._finished_at = record.finished_at
        operation._error = record.error
        operation._restored = True
        return operation


    def _register_task(self, task: "OperationTask") -> None:
        self._child_tasks[task.task_id] = task


    def _event_belongs_to_task(self, event: Event, task_id: UUID) -> bool:
        current_id = event.task_id
        visited: set[UUID] = set()
        while current_id is not None and current_id not in visited:
            if current_id == task_id:
                return True
            visited.add(current_id)
            task = self._child_tasks.get(current_id)
            current_id = task.parent_task_id if task is not None else None
        return False


    async def _read_task_terminal_event(self, task_id: UUID) -> Event | None:
        return await self._store.read_task_terminal_event(
            str(self.operation_id),
            str(task_id),
        )


    async def _persist_task(self, task: "OperationTask") -> None:
        """Persist one owned task before its worker starts."""
        await self._store.register_task(
            task_id=str(task.task_id),
            operation_id=str(self.operation_id),
            name=task.name,
            is_root=task.is_root,
            retry_policy=task.retry_policy.value,
            retry_input=task.retry_input,
            attempt=task.attempt,
            retry_of_operation_id=(
                str(task.retry_of_operation_id)
                if task.retry_of_operation_id is not None
                else None
            ),
            retry_of_task_id=(
                str(task.retry_of_task_id)
                if task.retry_of_task_id is not None
                else None
            ),
            created_at=task._created_at,
        )
        if task.retry_policy != RetryPolicy.NEVER and task.retry_input is not None:
            await self._stream.sync()


    @staticmethod
    def _parse_task_id(task_id: UUID | str) -> UUID:
        try:
            return task_id if isinstance(task_id, UUID) else UUID(task_id)
        except (TypeError, ValueError) as exc:
            raise RavenError(
                ErrorCode.OPERATION_TASK_NOT_FOUND,
                "task_id must be a valid UUID.",
            ) from exc


    @staticmethod
    def _parse_status(
        status: OperationStatus | str | None,
    ) -> OperationStatus | None:
        if status is None or isinstance(status, OperationStatus):
            return status
        try:
            return OperationStatus(status)
        except (TypeError, ValueError) as exc:
            raise RavenError(
                ErrorCode.INVALID_OPERATION_STATUS,
                f"Unknown operation status '{status}'.",
            ) from exc


    @staticmethod
    def _validate_page_size(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise RavenError(
                ErrorCode.INVALID_OPERATION_PAGE_SIZE,
                "Operation page size must be a positive integer.",
            )


    def _restore(self, events: list[Event]) -> None:
        started = next(
            (event for event in events if event.type == EventType.OPERATION_STARTED),
            None,
        )
        final = next((event for event in events if event.is_final), None)
        if started is not None:
            self._status = OperationStatus.RUNNING
            self._started_at = started.timestamp
        if final is None:
            return

        self._finished_at = final.timestamp
        if final.type == EventType.OPERATION_COMPLETED:
            self._status = OperationStatus.COMPLETED
        elif final.type == EventType.OPERATION_FAILED:
            self._status = OperationStatus.FAILED
            error = final.data.get("error")
            self._error = error if isinstance(error, dict) else None
        elif final.type == EventType.OPERATION_CANCELLED:
            self._status = OperationStatus.CANCELLED



class OperationTask:
    """Handle and result container for one invocation inside an operation."""

    def __init__(
        self,
        operation: Operation,
        name: str,
        worker: TaskWorker,
        *,
        root: bool,
        parent_task_id: UUID | None,
        retry_policy: RetryPolicy,
        retry_input: dict[str, Any] | None,
        attempt: int,
        retry_of_operation_id: UUID | None,
        retry_of_task_id: UUID | None,
        task_id: UUID | None = None,
    ) -> None:
        self.operation = operation
        self.task_id = task_id or uuid4()
        self.name = name
        self._worker = worker
        self._root = root
        self._parent_task_id = parent_task_id
        self._retry_policy = retry_policy
        self._retry_input = retry_input
        self._attempt = attempt
        self._retry_of_operation_id = retry_of_operation_id
        self._retry_of_task_id = retry_of_task_id
        self._created_at = datetime.now(timezone.utc)
        self._status = OperationStatus.QUEUED
        self._result: Any = None
        self._error: BaseException | None = None
        self._task: asyncio.Task[Any] | None = None
        self._result_observed = False


    @property
    def operation_id(self) -> UUID:
        return self.operation.operation_id


    @property
    def status(self) -> OperationStatus:
        return self._status


    @property
    def result_value(self) -> Any:
        """Return a completed result without waiting; prefer ``result()``."""
        return self._result


    @property
    def error(self) -> BaseException | None:
        return self._error


    @property
    def is_finished(self) -> bool:
        return self._status in _TERMINAL_STATUSES


    @property
    def is_root(self) -> bool:
        return self._root


    @property
    def parent_task_id(self) -> UUID | None:
        return self._parent_task_id


    @property
    def result_observed(self) -> bool:
        return self._result_observed


    @property
    def retry_policy(self) -> RetryPolicy:
        return self._retry_policy


    @property
    def retry_input(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._retry_input)


    @property
    def attempt(self) -> int:
        return self._attempt


    @property
    def retry_of_operation_id(self) -> UUID | None:
        return self._retry_of_operation_id


    @property
    def retry_of_task_id(self) -> UUID | None:
        return self._retry_of_task_id


    async def _persist(self) -> bool:
        persistence = asyncio.create_task(self.operation._persist_task(self))
        _, cancellation_requested = await await_completion(persistence)
        return cancellation_requested


    async def start(self) -> None:
        """Start a child task. Root tasks are started by Operation.start."""
        if self._root:
            raise RuntimeError("root operation tasks are started by Operation.start")
        if self._task is not None:
            raise RuntimeError(f"task '{self.task_id}' has already started")
        cancellation_requested = await self._publish_lifecycle(
            EventType.OPERATION_TASK_QUEUED
        )
        if cancellation_requested:
            await self._finalize_cancelled()
            raise asyncio.CancelledError
        self._task = asyncio.create_task(
            self._run(),
            name=f"nraven-operation-task-{self.task_id}",
        )


    async def _run_root(self, operation: Operation) -> Any:
        cancellation_requested = await self._publish_lifecycle(
            EventType.OPERATION_TASK_QUEUED
        )
        if cancellation_requested:
            await self._finalize_cancelled()
            raise asyncio.CancelledError
        return await self._run()


    async def _run(self) -> Any:
        token: Token[tuple[UUID, str] | None] = _CURRENT_TASK.set(
            (self.task_id, self.name)
        )
        try:
            cancellation_requested = await self._publish_lifecycle(
                EventType.OPERATION_TASK_STARTED
            )
            self._status = OperationStatus.RUNNING
            if cancellation_requested:
                raise asyncio.CancelledError
            self.operation.raise_if_cancelled()
            result = await self._worker(self.operation)
            cancellation_requested = await self._publish_lifecycle(
                EventType.OPERATION_TASK_COMPLETED
            )
            self._result = result
            self._status = OperationStatus.COMPLETED
            if cancellation_requested:
                raise asyncio.CancelledError
            return self._result
        except asyncio.CancelledError as exc:
            await self._finalize_cancelled(exc)
            if self._root:
                raise
            return None
        except Exception as exc:
            cancellation_requested = False
            try:
                cancellation_requested = await self._publish_lifecycle(
                    EventType.OPERATION_TASK_FAILED,
                    {"error": error_payload(exc)},
                )
            finally:
                self._status = OperationStatus.FAILED
                self._error = exc
            if cancellation_requested:
                raise asyncio.CancelledError
            if self._root:
                raise
            return None
        finally:
            _CURRENT_TASK.reset(token)


    async def result(self) -> Any:
        """Wait for this invocation and return or raise its native result."""
        if self._root:
            await self.operation.wait()
        elif self._task is not None:
            await asyncio.shield(self._task)
        self._result_observed = True
        if self._status == OperationStatus.CANCELLED:
            raise RavenError(
                ErrorCode.OPERATION_CANCELLED,
                f"Operation task '{self.task_id}' was cancelled.",
            )
        if self._error is not None:
            raise self._error
        if self._root and self.operation.status == OperationStatus.FAILED:
            if self.operation._terminal_exception is not None:
                raise self.operation._terminal_exception
            payload = self.operation.error or {}
            code_value = payload.get("code")
            try:
                code = ErrorCode(code_value)
            except (TypeError, ValueError):
                code = ErrorCode.INTERNAL_ERROR
            message = payload.get("message")
            raise RavenError(
                code,
                message if isinstance(message, str) else "The operation failed.",
                details=(
                    payload.get("details")
                    if isinstance(payload.get("details"), dict)
                    else None
                ),
            )
        return self._result


    async def cancel(self) -> None:
        """Cancel this task, or its root operation when it is the root task."""
        if self._root:
            await self.operation.cancel()
            return
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not self.is_finished:
            await self._finalize_cancelled()


    async def events(self, after_event_id: int = 0) -> AsyncIterator[Event]:
        """Yield events belonging to this task and its descendants."""
        if self._root:
            async for event in self.operation.events(
                after_event_id=after_event_id
            ):
                yield event
                if event.is_final:
                    return
            return

        terminal_event = await self.operation._read_task_terminal_event(self.task_id)
        if (
            terminal_event is not None
            and terminal_event.event_id is not None
            and terminal_event.event_id <= after_event_id
        ):
            return

        async for event in self.operation.events(after_event_id=after_event_id):
            if self.operation._event_belongs_to_task(event, self.task_id):
                yield event
                if (
                    event.task_id == self.task_id
                    and event.type in _TERMINAL_TASK_EVENT_TYPES
                ):
                    return


    async def _wait_for_worker(self) -> None:
        task = self._task
        if task is not None:
            await asyncio.shield(task)


    async def _finalize_cancelled(
        self,
        error: asyncio.CancelledError | None = None,
    ) -> None:
        if self.is_finished:
            return
        cancellation = error or asyncio.CancelledError()
        try:
            await self._publish_lifecycle(EventType.OPERATION_TASK_CANCELLED)
        finally:
            self._status = OperationStatus.CANCELLED
            self._error = cancellation


    async def _publish_lifecycle(
        self,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> bool:
        publication = asyncio.create_task(
            self.operation.publish(
                Event(
                    type=event_type,
                    data={"name": self.name, **(data or {})},
                    task_id=self.task_id,
                    task_name=self.name,
                )
            )
        )
        _, cancellation_requested = await await_completion(publication)
        return cancellation_requested



class OperationManager:
    """Own operation persistence, streams, lifecycle, and recovery."""

    def __init__(
        self,
        paths: PathConfig,
        *,
        sync_interval: float = OPERATION_SYNC_INTERVAL_SECONDS,
        max_cached_finished_operations: int = (
            DEFAULT_FINISHED_OPERATION_CACHE_SIZE
        ),
        event_replay_page_size: int = DEFAULT_EVENT_REPLAY_PAGE_SIZE,
    ) -> None:
        if (
            isinstance(sync_interval, bool)
            or not isinstance(sync_interval, (int, float))
            or not math.isfinite(sync_interval)
            or sync_interval <= 0
        ):
            raise RavenError(
                ErrorCode.INVALID_OPERATION_SYNC_INTERVAL,
                "Operation synchronization interval must be a finite positive number.",
            )
        if (
            isinstance(max_cached_finished_operations, bool)
            or not isinstance(max_cached_finished_operations, int)
            or max_cached_finished_operations < 0
        ):
            raise RavenError(
                ErrorCode.INVALID_OPERATION_CACHE_SIZE,
                "Finished operation cache size must be a non-negative integer.",
            )
        EventStream._validate_page_size(event_replay_page_size)
        self.storage_dir = paths.operation_storage_dir
        self.database_path = paths.operation_database_path
        self.sync_interval = float(sync_interval)
        self.max_cached_finished_operations = max_cached_finished_operations
        self.event_replay_page_size = event_replay_page_size
        self._store = SQLiteOperationStore(self.database_path)
        self._operations: OrderedDict[str, Operation] = OrderedDict()
        self._sync_service = OperationSyncService(self, self.sync_interval)
        self._sync_error: RavenError | None = None
        self._lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._closed = False


    @property
    def is_healthy(self) -> bool:
        return self._sync_error is None


    async def start(self) -> None:
        """Open operation storage and start periodic durable checkpoints."""
        async with self._lifecycle_lock:
            self._ensure_open()
            self._ensure_sync_healthy()
            if self._started:
                return
            await self._store.start()
            await self._sync_service.start()
            self._started = True


    async def recover(self) -> list[Operation]:
        """Finalize persisted non-terminal operations left by an earlier process."""
        async with self._recovery_lock:
            self._ensure_open()
            await self.start()
            unfinished_ids = await self._store.unfinished_operation_ids()
            recovered: list[Operation] = []

            for stored_id in unfinished_ids:
                operation_id = self._parse_operation_id(stored_id)
                key = str(operation_id)
                async with self._lock:
                    operation = self._operations.get(key)

                if operation is None:
                    stored = await self._store.read_operation(stored_id)
                    if stored is None:
                        continue
                    operation = Operation.from_record(
                        OperationRecord.model_validate(stored),
                        stream=self._create_stream(stored_id),
                        store=self._store,
                        on_finished=self._operation_finished,
                    )
                elif not operation._restored or operation._task is not None:
                    continue

                if operation.is_finished:
                    continue

                await operation._recover_interrupted_tasks()
                await operation._recover_interrupted()
                await self._remember_operation(operation)
                recovered.append(operation)

            return recovered


    async def create(self, name: str) -> Operation:
        """Create, persist, and register a queued operation."""
        if not isinstance(name, str) or not name.strip():
            raise RavenError(
                ErrorCode.INVALID_OPERATION_NAME,
                "Operation name cannot be empty.",
            )
        self._ensure_open()
        await self.start()
        operation_id = uuid4()
        key = str(operation_id)
        async with self._lock:
            self._ensure_open()
            operation = Operation(
                operation_id=operation_id,
                name=name,
                stream=self._create_stream(key),
                store=self._store,
                on_finished=self._operation_finished,
            )
            self._operations[key] = operation

        try:
            await operation._persist_queued()
        except BaseException:
            async with self._lock:
                self._operations.pop(key, None)
            raise
        return operation


    async def get(self, operation_id: UUID | str) -> Operation:
        self._ensure_open()
        await self.start()
        parsed_id = self._parse_operation_id(operation_id)
        key = str(parsed_id)
        async with self._lock:
            operation = self._operations.get(key)
            if operation is not None:
                self._operations.move_to_end(key)
                return operation

        stored = await self._store.read_operation(key)
        if stored is None:
            raise RavenError(
                ErrorCode.OPERATION_NOT_FOUND,
                f"Operation '{key}' was not found.",
            )
        operation = Operation.from_record(
            OperationRecord.model_validate(stored),
            stream=self._create_stream(key),
            store=self._store,
            on_finished=self._operation_finished,
        )
        return await self._remember_operation(operation)


    async def get_record(self, operation_id: UUID | str) -> OperationRecord:
        """Load one durable operation record without replaying its events."""
        self._ensure_open()
        await self.start()
        parsed_id = self._parse_operation_id(operation_id)
        stored = await self._store.read_operation(str(parsed_id))
        if stored is None:
            raise RavenError(
                ErrorCode.OPERATION_NOT_FOUND,
                f"Operation '{parsed_id}' was not found.",
            )
        return OperationRecord.model_validate(stored)


    async def wait(self, operation_id: UUID | str) -> Operation:
        operation = await self.get(operation_id)
        return await operation.wait()


    async def cancel(self, operation_id: UUID | str) -> Operation:
        operation = await self.get(operation_id)
        return await operation.cancel()


    async def events(
        self,
        operation_id: UUID | str,
        *,
        after_event_id: int = 0,
    ) -> AsyncIterator[Event]:
        operation = await self.get(operation_id)
        async for event in operation.events(after_event_id=after_event_id):
            yield event


    async def read_events(
        self,
        operation_id: UUID | str,
        *,
        after_event_id: int = 0,
        limit: int | None = None,
    ) -> list[Event]:
        operation = await self.get(operation_id)
        return await operation.read_events(
            after_event_id=after_event_id,
            limit=limit,
        )


    async def list_operations(
        self,
        *,
        status: OperationStatus | str | None = None,
        limit: int = DEFAULT_OPERATION_PAGE_SIZE,
        after_operation_id: UUID | str | None = None,
    ) -> list[OperationRecord]:
        """List durable operations newest first without replaying their events."""
        self._ensure_open()
        await self.start()
        parsed_status = self._parse_status(status)
        parsed_cursor: UUID | None = None
        if after_operation_id is not None:
            parsed_cursor = self._parse_operation_id(after_operation_id)
            if await self._store.read_operation(str(parsed_cursor)) is None:
                raise RavenError(
                    ErrorCode.OPERATION_NOT_FOUND,
                    f"Operation '{parsed_cursor}' was not found.",
                )
        self._validate_page_size(limit)
        records = await self._store.list_operations(
            status=parsed_status.value if parsed_status is not None else None,
            after_operation_id=(
                str(parsed_cursor) if parsed_cursor is not None else None
            ),
            limit=limit,
        )
        return [OperationRecord.model_validate(record) for record in records]


    async def list_retryable_tasks(
        self,
        *,
        limit: int = DEFAULT_OPERATION_PAGE_SIZE,
        after_task_id: UUID | str | None = None,
    ) -> list[OperationTaskRecord]:
        """List failed tasks currently eligible for user-confirmed retry."""
        self._ensure_open()
        await self.start()
        self._validate_page_size(limit)
        parsed_cursor: UUID | None = None
        if after_task_id is not None:
            parsed_cursor = Operation._parse_task_id(after_task_id)
            if await self._store.read_task(str(parsed_cursor)) is None:
                raise RavenError(
                    ErrorCode.OPERATION_TASK_NOT_FOUND,
                    f"Operation task '{parsed_cursor}' was not found.",
                )

        results: list[OperationTaskRecord] = []
        cursor = str(parsed_cursor) if parsed_cursor is not None else None
        while len(results) < limit:
            records = await self._store.list_tasks(
                operation_id=None,
                status=OperationStatus.FAILED.value,
                after_task_id=cursor,
                limit=limit,
                retryable_candidates_only=True,
            )
            if not records:
                break
            validated = [
                OperationTaskRecord.model_validate(record) for record in records
            ]
            for record in validated:
                if record.can_retry:
                    results.append(record)
                    if len(results) == limit:
                        break
            cursor = str(validated[-1].task_id)
            if len(records) < limit:
                break
        return results


    async def stored_operation_ids(self) -> list[str]:
        """Return every operation ID persisted for this user."""
        self._ensure_open()
        await self.start()
        return await self._store.operation_ids()


    async def unfinished_operation_ids(self) -> list[str]:
        """Return persisted operations without a terminal event."""
        self._ensure_open()
        await self.start()
        return await self._store.unfinished_operation_ids()


    async def sync_dirty(self) -> list[str]:
        """Checkpoint dirty operation data and return affected loaded IDs."""
        self._ensure_open()
        self._ensure_sync_healthy()
        async with self._lock:
            dirty_operation_ids = [
                operation_id
                for operation_id, operation in self._operations.items()
                if operation._stream.is_dirty
            ]
        if not self._store.is_dirty:
            return []

        try:
            await self._store.checkpoint()
        except RavenError as exc:
            await self._record_sync_failure(exc)
            raise
        return dirty_operation_ids


    async def cancel_active(self) -> None:
        """Cancel and await active operations without closing event storage."""
        async with self._lock:
            operations = [
                operation
                for operation in self._operations.values()
                if not operation.is_finished
            ]

        await asyncio.gather(
            *(operation.cancel() for operation in operations),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(operation.wait() for operation in operations),
            return_exceptions=True,
        )


    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True

        await self.cancel_active()
        await self._sync_service.close()

        close_error: BaseException | None = None
        if self._store.is_dirty:
            try:
                await self._store.checkpoint()
            except BaseException as exc:
                close_error = exc

        async with self._lock:
            streams = [operation._stream for operation in self._operations.values()]
        stream_results = await asyncio.gather(
            *(stream.close() for stream in streams),
            return_exceptions=True,
        )
        await self._store.close()

        if close_error is not None:
            raise close_error
        for result in stream_results:
            if isinstance(result, BaseException):
                raise result


    def _create_stream(self, operation_id: str) -> EventStream:
        return EventStream(
            operation_id=operation_id,
            store=self._store,
            health_check=self._ensure_sync_healthy,
            sync_failure=self._record_sync_failure,
            replay_page_size=self.event_replay_page_size,
        )


    async def _remember_operation(self, operation: Operation) -> Operation:
        key = str(operation.operation_id)
        async with self._lock:
            current = self._operations.get(key)
            if current is not None:
                self._operations.move_to_end(key)
                return current
            self._operations[key] = operation
            self._operations.move_to_end(key)
            self._evict_finished_operations()
            return operation


    def _operation_finished(self, operation: Operation) -> None:
        key = str(operation.operation_id)
        if self._operations.get(key) is not operation:
            return
        self._operations.move_to_end(key)
        self._evict_finished_operations()


    def _evict_finished_operations(self) -> None:
        finished = [
            operation_id
            for operation_id, operation in self._operations.items()
            if operation.is_finished
        ]
        excess = len(finished) - self.max_cached_finished_operations
        for operation_id in finished[:max(excess, 0)]:
            self._operations.pop(operation_id, None)


    async def _delete_finished(self, operation_id: str) -> bool:
        async with self._lock:
            operation = self._operations.get(operation_id)
            if operation is not None:
                if not operation.is_finished:
                    return False
                if not await operation._stream._reserve_cleanup():
                    return False

            deletion = asyncio.create_task(self._store.delete(operation_id))
            try:
                deleted = await asyncio.shield(deletion)
            except asyncio.CancelledError:
                try:
                    deleted = await asyncio.shield(deletion)
                except BaseException:
                    if operation is not None:
                        await operation._stream._release_cleanup()
                    raise
                if deleted and operation is not None:
                    if self._operations.get(operation_id) is operation:
                        self._operations.pop(operation_id, None)
                elif operation is not None:
                    await operation._stream._release_cleanup()
                raise
            except BaseException:
                if operation is not None:
                    await operation._stream._release_cleanup()
                raise

            if not deleted:
                if operation is not None:
                    await operation._stream._release_cleanup()
                return False

            if operation is not None:
                if self._operations.get(operation_id) is operation:
                    self._operations.pop(operation_id, None)
            return True


    async def _expired_operation_ids(
        self,
        cutoff: datetime,
        *,
        limit: int,
    ) -> list[str]:
        self._ensure_open()
        await self.start()
        return await self._store.expired_operation_ids(cutoff, limit=limit)


    async def _record_sync_failure(
        self,
        error: RavenError,
        source_operation_id: str | None = None,
    ) -> None:
        if self._sync_error is None:
            self._sync_error = error
        active_error = self._sync_error or error
        async with self._lock:
            streams = [
                operation._stream
                for operation in self._operations.values()
                if str(operation.operation_id) != source_operation_id
            ]
        await asyncio.gather(
            *(stream.mark_sync_failed(active_error) for stream in streams)
        )


    def _ensure_sync_healthy(self) -> None:
        if self._sync_error is not None:
            raise self._sync_error


    @staticmethod
    def _parse_operation_id(operation_id: UUID | str) -> UUID:
        try:
            return operation_id if isinstance(operation_id, UUID) else UUID(operation_id)
        except (TypeError, ValueError) as exc:
            raise RavenError(
                ErrorCode.INVALID_OPERATION_ID,
                "operation_id must be a valid UUID.",
            ) from exc


    @staticmethod
    def _parse_status(
        status: OperationStatus | str | None,
    ) -> OperationStatus | None:
        return Operation._parse_status(status)


    @staticmethod
    def _validate_page_size(limit: int) -> None:
        Operation._validate_page_size(limit)


    def _ensure_open(self) -> None:
        if self._closed:
            raise RavenError(
                ErrorCode.OPERATION_MANAGER_CLOSED,
                "Operation manager is closed.",
            )



class OperationSyncService:
    """Periodically checkpoint one manager's dirty operation database."""

    def __init__(self, manager: OperationManager, interval: float) -> None:
        self._manager = manager
        self.interval = interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False


    async def start(self) -> None:
        if self._closed:
            raise RavenError(
                ErrorCode.OPERATION_MANAGER_CLOSED,
                "Operation synchronization service is closed.",
            )
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(),
            name="nraven-operation-sync",
        )


    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)


    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass

            if self._stop.is_set():
                return

            try:
                await self._manager.sync_dirty()
            except RavenError:
                return



class OperationCleanupService:
    """Delete a bounded batch of finished operations after retention expires.

    Retention applies to complete operation records, including retry inputs and
    events. A task must therefore be retried before its source operation expires.
    Retry operations are retained according to their own completion timestamps.
    """

    def __init__(
        self,
        manager: OperationManager,
        retention: timedelta,
        *,
        batch_size: int = DEFAULT_OPERATION_CLEANUP_BATCH_SIZE,
    ) -> None:
        if retention < timedelta(0):
            raise RavenError(
                ErrorCode.INVALID_RETENTION,
                "retention cannot be negative.",
            )
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or batch_size <= 0
        ):
            raise RavenError(
                ErrorCode.INVALID_CLEANUP_BATCH_SIZE,
                "Cleanup batch size must be a positive integer.",
            )
        self._manager = manager
        self.retention = retention
        self.batch_size = batch_size
        self._lock = asyncio.Lock()


    async def run_once(
        self,
        now: datetime | None = None,
    ) -> OperationCleanupResult:
        """Attempt one cleanup batch without aborting on an individual failure."""
        async with self._lock:
            current_time = now or datetime.now(timezone.utc)
            if current_time.tzinfo is None:
                current_time = current_time.replace(tzinfo=timezone.utc)
            cutoff = current_time.astimezone(timezone.utc) - self.retention
            operation_ids = await self._manager._expired_operation_ids(
                cutoff,
                limit=self.batch_size,
            )

            deleted: list[str] = []
            skipped: list[str] = []
            failures: dict[str, dict[str, Any]] = {}
            for operation_id in operation_ids:
                try:
                    was_deleted = await self._manager._delete_finished(operation_id)
                except Exception as exc:
                    failures[operation_id] = error_payload(exc)
                    continue

                if was_deleted:
                    deleted.append(operation_id)
                else:
                    skipped.append(operation_id)

            return OperationCleanupResult(
                deleted_operation_ids=tuple(deleted),
                skipped_operation_ids=tuple(skipped),
                failures=failures,
            )
