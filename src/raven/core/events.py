"""Persistent per-operation event streams for Raven."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .errors import ErrorCode, RavenError


class EventType(StrEnum):
    """Event names emitted by Raven components."""

    CHAT_DELTA = "chat.delta"
    CHAT_THINKING_DELTA = "chat.thinking_delta"
    CHAT_RESPONSE_DELTA = "chat.response_delta"
    CHAT_TOOL_CALL = "chat.tool_call"
    CHAT_TOOL_RESULT = "chat.tool_result"
    CHAT_MAX_ITERATIONS = "chat.max_iterations"
    CHAT_COMPLETED = "chat.completed"
    CHAT_FAILED = "chat.failed"
    WORK_PROGRESS = "work.progress"

    INGESTION_STARTED = "ingestion.started"
    INGESTION_PROGRESS = "ingestion.progress"
    INGESTION_VECTORS_WRITTEN = "ingestion.vectors_written"
    INGESTION_METADATA_COMMITTED = "ingestion.metadata_committed"
    INGESTION_CLEANUP_STARTED = "ingestion.cleanup.started"
    INGESTION_CLEANUP_COMPLETED = "ingestion.cleanup.completed"
    INGESTION_CLEANUP_FAILED = "ingestion.cleanup.failed"
    INGESTION_COMPLETED = "ingestion.completed"
    INGESTION_FAILED = "ingestion.failed"

    KNOWLEDGE_CREATE_STARTED = "knowledge.create.started"
    KNOWLEDGE_CREATE_COMPLETED = "knowledge.create.completed"
    KNOWLEDGE_CREATE_FAILED = "knowledge.create.failed"
    KNOWLEDGE_DELETE_STARTED = "knowledge.delete.started"
    KNOWLEDGE_DELETE_COMPLETED = "knowledge.delete.completed"
    KNOWLEDGE_DELETE_FAILED = "knowledge.delete.failed"
    KNOWLEDGE_UPDATED = "knowledge.updated"
    KNOWLEDGE_INGEST_STARTED = "knowledge.ingest.started"
    KNOWLEDGE_INGEST_PROGRESS = "knowledge.ingest.progress"
    KNOWLEDGE_INGEST_COMPLETED = "knowledge.ingest.completed"
    KNOWLEDGE_INGEST_FAILED = "knowledge.ingest.failed"
    KNOWLEDGE_FILE_DELETE_STARTED = "knowledge.file_delete.started"
    KNOWLEDGE_FILE_DELETE_COMPLETED = "knowledge.file_delete.completed"
    KNOWLEDGE_FILE_DELETE_FAILED = "knowledge.file_delete.failed"

    CONVERSATION_CREATE_STARTED = "conversation.create.started"
    CONVERSATION_CREATE_COMPLETED = "conversation.create.completed"
    CONVERSATION_CREATE_FAILED = "conversation.create.failed"
    CONVERSATION_UPDATE_STARTED = "conversation.update.started"
    CONVERSATION_UPDATE_COMPLETED = "conversation.update.completed"
    CONVERSATION_UPDATE_FAILED = "conversation.update.failed"
    CONVERSATION_DELETE_STARTED = "conversation.delete.started"
    CONVERSATION_DELETE_COMPLETED = "conversation.delete.completed"
    CONVERSATION_DELETE_FAILED = "conversation.delete.failed"
    CONVERSATION_MEMORY_COMPACTION_STARTED = "conversation.memory_compaction.started"
    CONVERSATION_MEMORY_COMPACTION_COMPLETED = "conversation.memory_compaction.completed"
    CONVERSATION_MEMORY_COMPACTION_FAILED = "conversation.memory_compaction.failed"
    CONVERSATION_TURN_COMMIT_STARTED = "conversation.turn_commit.started"
    CONVERSATION_TURN_COMMITTED = "conversation.turn_commit.completed"
    CONVERSATION_TURN_REUSED = "conversation.turn_commit.reused"
    CONVERSATION_TURN_COMMIT_FAILED = "conversation.turn_commit.failed"
    CONVERSATION_MEMORY_INDEX_STARTED = "conversation.memory_index.started"
    CONVERSATION_MEMORY_INDEX_COMPLETED = "conversation.memory_index.completed"
    CONVERSATION_MEMORY_INDEX_FAILED = "conversation.memory_index.failed"

    RETRIEVAL_EMBEDDED_STARTED = "retrieval.embedded.started"
    RETRIEVAL_EMBEDDED_COMPLETED = "retrieval.embedded.completed"
    RETRIEVAL_EMBEDDED_FAILED = "retrieval.embedded.failed"
    RETRIEVAL_HIERARCHICAL_STARTED = "retrieval.hierarchical.started"
    RETRIEVAL_HIERARCHICAL_READ = "retrieval.hierarchical.read"
    RETRIEVAL_HIERARCHICAL_COMPLETED = "retrieval.hierarchical.completed"
    RETRIEVAL_HIERARCHICAL_FAILED = "retrieval.hierarchical.failed"
    RETRIEVAL_AGREEMENT_STARTED = "retrieval.agreement.started"
    RETRIEVAL_AGREEMENT_COMPLETED = "retrieval.agreement.completed"
    RETRIEVAL_AGREEMENT_FAILED = "retrieval.agreement.failed"
    RETRIEVAL_VECTOR_CONDITIONED_STARTED = "retrieval.vector_conditioned.started"
    RETRIEVAL_VECTOR_CONDITIONED_COMPLETED = "retrieval.vector_conditioned.completed"
    RETRIEVAL_VECTOR_CONDITIONED_FAILED = "retrieval.vector_conditioned.failed"

    RECONSTRUCTION_STARTED = "reconstruction.started"
    RECONSTRUCTION_FILE = "reconstruction.file"
    RECONSTRUCTION_COMPLETED = "reconstruction.completed"
    RECONSTRUCTION_FAILED = "reconstruction.failed"

    OPERATION_QUEUED = "operation.queued"
    OPERATION_STARTED = "operation.started"
    OPERATION_COMPLETED = "operation.completed"
    OPERATION_FAILED = "operation.failed"
    OPERATION_CANCELLED = "operation.cancelled"
    OPERATION_TASK_QUEUED = "operation.task.queued"
    OPERATION_TASK_STARTED = "operation.task.started"
    OPERATION_TASK_COMPLETED = "operation.task.completed"
    OPERATION_TASK_FAILED = "operation.task.failed"
    OPERATION_TASK_CANCELLED = "operation.task.cancelled"

    MODEL_CONNECTION_STARTED = "model.connection.started"
    MODEL_CONNECTION_COMPLETED = "model.connection.completed"
    MODEL_CONNECTION_FAILED = "model.connection.failed"
    MODEL_LIST_STARTED = "model.list.started"
    MODEL_LIST_COMPLETED = "model.list.completed"
    MODEL_LIST_FAILED = "model.list.failed"
    MODEL_INSPECT_STARTED = "model.inspect.started"
    MODEL_INSPECT_COMPLETED = "model.inspect.completed"
    MODEL_INSPECT_FAILED = "model.inspect.failed"
    MODEL_PULL_STARTED = "model.pull.started"
    MODEL_PULL_PROGRESS = "model.pull.progress"
    MODEL_PULL_COMPLETED = "model.pull.completed"
    MODEL_PULL_FAILED = "model.pull.failed"
    MODEL_DELETE_STARTED = "model.delete.started"
    MODEL_DELETE_COMPLETED = "model.delete.completed"
    MODEL_DELETE_FAILED = "model.delete.failed"
    MODEL_LOAD_LLM_STARTED = "model.load_llm.started"
    MODEL_LOAD_LLM_COMPLETED = "model.load_llm.completed"
    MODEL_LOAD_LLM_FAILED = "model.load_llm.failed"
    MODEL_LOAD_EMBEDDING_STARTED = "model.load_embedding.started"
    MODEL_LOAD_EMBEDDING_COMPLETED = "model.load_embedding.completed"
    MODEL_LOAD_EMBEDDING_FAILED = "model.load_embedding.failed"
    MODEL_UNLOAD_LLM_STARTED = "model.unload_llm.started"
    MODEL_UNLOAD_LLM_COMPLETED = "model.unload_llm.completed"
    MODEL_UNLOAD_LLM_FAILED = "model.unload_llm.failed"
    MODEL_UNLOAD_EMBEDDING_STARTED = "model.unload_embedding.started"
    MODEL_UNLOAD_EMBEDDING_COMPLETED = "model.unload_embedding.completed"
    MODEL_UNLOAD_EMBEDDING_FAILED = "model.unload_embedding.failed"



_IMMEDIATE_SYNC_EVENT_TYPES = {
    EventType.OPERATION_QUEUED,
    EventType.OPERATION_STARTED,
    EventType.OPERATION_COMPLETED,
    EventType.OPERATION_FAILED,
    EventType.OPERATION_CANCELLED,
}

class Event(BaseModel):
    """An event stored in an operation's event stream."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)
    operation_id: UUID | None = None
    task_id: UUID | None = None
    task_name: str | None = None
    event_id: int | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_final: bool = False



class EventStore(Protocol):
    """Storage behavior required by an event stream."""

    @property
    def is_dirty(self) -> bool: ...


    @property
    def synced_generation(self) -> int: ...


    async def append(self, event: Event) -> int: ...


    async def read_after(
        self,
        operation_id: str,
        after_event_id: int,
    ) -> list[Event]: ...


    async def read_metadata(
        self,
        operation_id: str,
    ) -> tuple[int, bool, datetime | None]: ...


    async def checkpoint(self) -> bool: ...



class EventStream:
    """The persistent event stream belonging to one operation."""

    def __init__(
        self,
        operation_id: str,
        store: EventStore,
        health_check: Callable[[], None] | None = None,
        sync_failure: Callable[[RavenError, str | None], Awaitable[None]] | None = None,
    ) -> None:
        self.operation_id = operation_id
        self._store = store
        self._health_check = health_check
        self._sync_failure = sync_failure
        self._condition = asyncio.Condition()
        self._last_event_id = 0
        self._last_write_generation = 0
        self._finished = False
        self._finished_at: datetime | None = None
        self._sync_error: RavenError | None = None
        self._loaded = False
        self._closed = False


    @property
    def is_dirty(self) -> bool:
        return self._last_write_generation > self._store.synced_generation


    async def publish(self, event: Event) -> Event:
        """Persist an event and synchronize lifecycle boundaries immediately."""
        self._ensure_open()
        self._raise_if_unhealthy()

        async with self._condition:
            await self._load()
            self._raise_if_unhealthy()
            if self._finished:
                raise RavenError(
                    ErrorCode.EVENT_STREAM_FINISHED,
                    f"Operation '{self.operation_id}' is already finished.",
                )

            persisted = event.model_copy(
                update={
                    "operation_id": UUID(self.operation_id),
                    "event_id": self._last_event_id + 1,
                }
            )
            self._last_write_generation = await self._store.append(persisted)

            if persisted.is_final or persisted.type in _IMMEDIATE_SYNC_EVENT_TYPES:
                await self._sync_locked()

            self._last_event_id = persisted.event_id or self._last_event_id
            self._finished = persisted.is_final
            if persisted.is_final:
                self._finished_at = persisted.timestamp
            self._condition.notify_all()
            return persisted


    async def read(self, after_event_id: int = 0) -> list[Event]:
        """Read this stream's retained events after a cursor."""
        self._validate_cursor(after_event_id)

        async with self._condition:
            await self._load()
            self._raise_if_unhealthy()
            self._validate_available_cursor(after_event_id)
            return await self._store.read_after(self.operation_id, after_event_id)


    async def events(self, after_event_id: int = 0) -> AsyncIterator[Event]:
        """Yield retained events after a cursor, then wait for new events."""
        self._validate_cursor(after_event_id)
        cursor = after_event_id

        async with self._condition:
            await self._load()
            self._raise_if_unhealthy()
            self._validate_available_cursor(after_event_id)

        while True:
            async with self._condition:
                await self._load()
                self._raise_if_unhealthy()
                events = await self._store.read_after(self.operation_id, cursor)

                if not events:
                    if self._finished or self._closed:
                        return
                    await self._condition.wait()
                    continue

            for event in events:
                cursor = event.event_id or cursor
                yield event


    async def sync(self) -> bool:
        """Checkpoint pending writes to durable storage."""
        self._ensure_open()

        async with self._condition:
            await self._load()
            return await self._sync_locked()


    async def mark_sync_failed(self, error: RavenError) -> None:
        """Make a manager-level synchronization failure visible to readers."""
        async with self._condition:
            if self._sync_error is None:
                self._sync_error = error
            self._condition.notify_all()


    async def close(self) -> None:
        """Checkpoint pending writes and stop live stream activity."""
        async with self._condition:
            if self._closed:
                return
            if self._store.is_dirty:
                await self._sync_locked()
            self._closed = True
            self._condition.notify_all()


    def _ensure_open(self) -> None:
        if self._closed:
            raise RavenError(
                ErrorCode.EVENT_STREAM_CLOSED,
                "Event stream is closed.",
            )


    def _raise_if_unhealthy(self) -> None:
        if self._health_check is not None:
            self._health_check()
        if self._sync_error is not None:
            raise self._sync_error


    @staticmethod
    def _validate_cursor(after_event_id: int) -> None:
        if after_event_id < 0:
            raise RavenError(
                ErrorCode.INVALID_EVENT_CURSOR,
                "after_event_id cannot be negative.",
            )


    def _validate_available_cursor(self, after_event_id: int) -> None:
        if after_event_id <= self._last_event_id:
            return
        raise RavenError(
            ErrorCode.EVENT_HISTORY_GAP,
            "The requested event cursor is ahead of the recovered event history.",
            details={
                "operation_id": self.operation_id,
                "requested_after_event_id": after_event_id,
                "available_last_event_id": self._last_event_id,
            },
        )


    async def _load(self) -> None:
        if self._loaded:
            return
        self._last_event_id, self._finished, self._finished_at = (
            await self._store.read_metadata(self.operation_id)
        )
        self._loaded = True


    async def _sync_locked(self) -> bool:
        if not self._store.is_dirty:
            return False

        try:
            return await self._store.checkpoint()
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, RavenError)
                and exc.code == ErrorCode.OPERATION_SYNC_FAILED
                else RavenError(
                    ErrorCode.OPERATION_SYNC_FAILED,
                    "The operation database could not be synchronized to durable storage.",
                )
            )
            self._sync_error = error
            if self._sync_failure is not None:
                await self._sync_failure(error, self.operation_id)
            self._condition.notify_all()
            raise error from exc
