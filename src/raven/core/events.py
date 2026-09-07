"""Persistent per-operation event streams for Raven."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .config import OPERATION_SYNC_INTERVAL_SECONDS, PathConfig
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

_OPERATION_STATUS_BY_EVENT = {
    EventType.OPERATION_QUEUED: "queued",
    EventType.OPERATION_STARTED: "running",
    EventType.OPERATION_COMPLETED: "completed",
    EventType.OPERATION_FAILED: "failed",
    EventType.OPERATION_CANCELLED: "cancelled",
}

_TASK_STATUS_BY_EVENT = {
    EventType.OPERATION_TASK_QUEUED: "queued",
    EventType.OPERATION_TASK_STARTED: "running",
    EventType.OPERATION_TASK_COMPLETED: "completed",
    EventType.OPERATION_TASK_FAILED: "failed",
    EventType.OPERATION_TASK_CANCELLED: "cancelled",
}

_TERMINAL_TASK_EVENT_TYPES = {
    EventType.OPERATION_TASK_COMPLETED,
    EventType.OPERATION_TASK_FAILED,
    EventType.OPERATION_TASK_CANCELLED,
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



class SQLiteOperationStore:
    """Persist one user's operations, tasks, and events in SQLite."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._write_generation = 0
        self._synced_generation = 0
        self._closed = False


    @property
    def is_dirty(self) -> bool:
        return self._write_generation > self._synced_generation


    @property
    def synced_generation(self) -> int:
        return self._synced_generation


    async def start(self) -> None:
        if self._closed:
            raise RavenError(
                ErrorCode.EVENT_STREAM_CLOSED,
                "Operation database is closed.",
            )
        if self._connection is not None:
            return

        async with self._start_lock:
            if self._connection is not None:
                return
            try:
                self._connection = await asyncio.to_thread(self._open)
            except RavenError:
                raise
            except Exception as exc:
                raise RavenError(
                    ErrorCode.OPERATION_DATABASE_FAILED,
                    "The operation database could not be opened.",
                ) from exc


    async def append(self, event: Event) -> int:
        await self.start()
        async with self._lock:
            try:
                await asyncio.to_thread(self._append, event)
            except RavenError:
                raise
            except Exception as exc:
                raise RavenError(
                    ErrorCode.OPERATION_DATABASE_FAILED,
                    "The event could not be persisted.",
                    details={"operation_id": str(event.operation_id)},
                ) from exc
            self._write_generation += 1
            return self._write_generation


    async def register_task(
        self,
        *,
        task_id: str,
        operation_id: str,
        name: str,
        is_root: bool,
        retry_policy: str,
        retry_input: dict[str, Any] | None,
        attempt: int,
        retry_of_operation_id: str | None,
        retry_of_task_id: str | None,
        created_at: datetime,
    ) -> int:
        await self.start()
        async with self._lock:
            try:
                await asyncio.to_thread(
                    self._register_task,
                    task_id,
                    operation_id,
                    name,
                    is_root,
                    retry_policy,
                    retry_input,
                    attempt,
                    retry_of_operation_id,
                    retry_of_task_id,
                    created_at.isoformat(),
                )
            except RavenError:
                raise
            except Exception as exc:
                raise self._database_error("The operation task could not be persisted.") from exc
            self._write_generation += 1
            return self._write_generation


    async def read_task(self, task_id: str) -> dict[str, Any] | None:
        await self.start()
        async with self._lock:
            try:
                row = await asyncio.to_thread(self._read_task, task_id)
            except Exception as exc:
                raise self._database_error("The operation task could not be read.") from exc
        if row is None:
            return None
        try:
            return self._task_from_row(row)
        except Exception as exc:
            raise RavenError(
                ErrorCode.OPERATION_DATABASE_CORRUPTED,
                "The operation database contains an invalid task record.",
                details={"task_id": task_id},
            ) from exc


    async def read_retry(self, task_id: str) -> dict[str, Any] | None:
        """Return the task that directly retries the supplied task, if any."""
        await self.start()
        async with self._lock:
            try:
                row = await asyncio.to_thread(self._read_retry, task_id)
            except Exception as exc:
                raise self._database_error("The operation retry could not be read.") from exc
        if row is None:
            return None
        try:
            return self._task_from_row(row)
        except Exception as exc:
            raise RavenError(
                ErrorCode.OPERATION_DATABASE_CORRUPTED,
                "The operation database contains an invalid retry task record.",
                details={"retry_of_task_id": task_id},
            ) from exc


    async def unfinished_tasks(self, operation_id: str) -> list[dict[str, Any]]:
        await self.start()
        async with self._lock:
            try:
                rows = await asyncio.to_thread(self._unfinished_tasks, operation_id)
            except Exception as exc:
                raise self._database_error("Unfinished operation tasks could not be read.") from exc
        try:
            return [self._task_from_row(row) for row in rows]
        except Exception as exc:
            raise RavenError(
                ErrorCode.OPERATION_DATABASE_CORRUPTED,
                "The operation database contains an invalid task record.",
                details={"operation_id": operation_id},
            ) from exc


    async def read_metadata(
        self,
        operation_id: str,
    ) -> tuple[int, bool, datetime | None]:
        await self.start()
        async with self._lock:
            try:
                row = await asyncio.to_thread(self._read_metadata, operation_id)
            except Exception as exc:
                raise self._database_error("Event metadata could not be read.") from exc

        if row is None:
            return 0, False, None
        last_event_id, is_finished, finished_at = row
        return (
            int(last_event_id),
            bool(is_finished),
            datetime.fromisoformat(str(finished_at)) if finished_at else None,
        )


    async def read_after(self, operation_id: str, after_event_id: int) -> list[Event]:
        await self.start()
        async with self._lock:
            try:
                rows = await asyncio.to_thread(
                    self._read_after,
                    operation_id,
                    after_event_id,
                )
            except Exception as exc:
                raise self._database_error("Events could not be read.") from exc

        try:
            return [self._event_from_row(row) for row in rows]
        except Exception as exc:
            raise RavenError(
                ErrorCode.OPERATION_DATABASE_CORRUPTED,
                "The operation database contains an invalid event record.",
                details={"operation_id": operation_id},
            ) from exc


    async def operation_ids(self) -> list[str]:
        await self.start()
        async with self._lock:
            try:
                return await asyncio.to_thread(self._operation_ids)
            except Exception as exc:
                raise self._database_error("Operation IDs could not be read.") from exc


    async def unfinished_operation_ids(self) -> list[str]:
        await self.start()
        async with self._lock:
            try:
                return await asyncio.to_thread(self._unfinished_operation_ids)
            except Exception as exc:
                raise self._database_error(
                    "Unfinished operation IDs could not be read."
                ) from exc


    async def expired_operation_ids(self, cutoff: datetime) -> list[str]:
        await self.start()
        async with self._lock:
            try:
                return await asyncio.to_thread(
                    self._expired_operation_ids,
                    cutoff.isoformat(),
                )
            except Exception as exc:
                raise self._database_error("Expired operations could not be read.") from exc


    async def delete(self, operation_id: str) -> None:
        await self.start()
        async with self._lock:
            try:
                changed = await asyncio.to_thread(self._delete, operation_id)
            except Exception as exc:
                raise self._database_error("The operation events could not be deleted.") from exc
            if changed:
                self._write_generation += 1


    async def checkpoint(self) -> bool:
        await self.start()
        async with self._lock:
            if not self.is_dirty:
                return False
            try:
                await asyncio.to_thread(self._checkpoint)
            except Exception as exc:
                raise RavenError(
                    ErrorCode.OPERATION_SYNC_FAILED,
                    "The operation database could not be synchronized to durable storage.",
                ) from exc
            self._synced_generation = self._write_generation
            return True


    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            connection = self._connection
            self._connection = None
            if connection is not None:
                await asyncio.to_thread(connection.close)


    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row

        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if journal_mode is None or str(journal_mode[0]).lower() != "wal":
                raise sqlite3.OperationalError("WAL mode could not be enabled")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_event_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    is_finished INTEGER NOT NULL CHECK (is_finished IN (0, 1))
                );

                CREATE TABLE IF NOT EXISTS events (
                    operation_id TEXT NOT NULL,
                    event_id INTEGER NOT NULL CHECK (event_id > 0),
                    type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    task_id TEXT,
                    task_name TEXT,
                    is_final INTEGER NOT NULL CHECK (is_final IN (0, 1)),
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (operation_id, event_id),
                    FOREIGN KEY (operation_id)
                        REFERENCES operations(operation_id)
                        ON DELETE CASCADE
                ) WITHOUT ROWID;

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY NOT NULL,
                    operation_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    is_root INTEGER NOT NULL CHECK (is_root IN (0, 1)),
                    status TEXT NOT NULL,
                    retry_policy TEXT NOT NULL,
                    retry_input_json TEXT,
                    attempt INTEGER NOT NULL CHECK (attempt > 0),
                    retry_of_operation_id TEXT,
                    retry_of_task_id TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error_json TEXT,
                    FOREIGN KEY (operation_id)
                        REFERENCES operations(operation_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS operations_finished_at
                    ON operations(finished_at)
                    WHERE finished_at IS NOT NULL;

                CREATE INDEX IF NOT EXISTS tasks_operation_id
                    ON tasks(operation_id, created_at);

                CREATE INDEX IF NOT EXISTS tasks_retry_of_task_id
                    ON tasks(retry_of_task_id)
                    WHERE retry_of_task_id IS NOT NULL;

                CREATE UNIQUE INDEX IF NOT EXISTS tasks_single_direct_retry
                    ON tasks(retry_of_task_id)
                    WHERE retry_of_task_id IS NOT NULL;

                PRAGMA user_version = 2;
                """
            )
            return connection
        except Exception:
            connection.close()
            raise


    def _register_task(
        self,
        task_id: str,
        operation_id: str,
        name: str,
        is_root: bool,
        retry_policy: str,
        retry_input: dict[str, Any] | None,
        attempt: int,
        retry_of_operation_id: str | None,
        retry_of_task_id: str | None,
        created_at: str,
    ) -> None:
        connection = self._require_connection()
        retry_input_json = (
            json.dumps(retry_input, separators=(",", ":"), allow_nan=False)
            if retry_input is not None
            else None
        )

        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id,
                    operation_id,
                    name,
                    is_root,
                    status,
                    retry_policy,
                    retry_input_json,
                    attempt,
                    retry_of_operation_id,
                    retry_of_task_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    operation_id,
                    name,
                    int(is_root),
                    "queued",
                    retry_policy,
                    retry_input_json,
                    attempt,
                    retry_of_operation_id,
                    retry_of_task_id,
                    created_at,
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            if connection.in_transaction:
                connection.rollback()
            if retry_of_task_id is not None:
                existing = self._read_retry(retry_of_task_id)
                details = {"retry_of_task_id": retry_of_task_id}
                if existing is not None:
                    details.update(
                        {
                            "existing_task_id": str(existing["task_id"]),
                            "existing_operation_id": str(existing["operation_id"]),
                            "existing_status": str(existing["status"]),
                        }
                    )
                raise RavenError(
                    ErrorCode.OPERATION_TASK_ALREADY_RETRIED,
                    f"Operation task '{retry_of_task_id}' already has a retry.",
                    details=details,
                ) from exc
            raise
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise


    def _append(self, event: Event) -> None:
        if event.operation_id is None or event.event_id is None:
            raise RavenError(
                ErrorCode.OPERATION_DATABASE_CORRUPTED,
                "A persisted event requires operation_id and event_id.",
            )

        connection = self._require_connection()
        operation_id = str(event.operation_id)
        serialized = event.model_dump(mode="json")
        timestamp = str(serialized["timestamp"])
        event_name = event.data.get("name")
        name = event_name if isinstance(event_name, str) else ""
        status = _OPERATION_STATUS_BY_EVENT.get(event.type)

        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT
                    name,
                    status,
                    last_event_id,
                    started_at,
                    finished_at,
                    is_finished
                FROM operations
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()

            if existing is None:
                if event.event_id != 1:
                    raise RavenError(
                        ErrorCode.OPERATION_DATABASE_CORRUPTED,
                        "The first persisted event must have event_id 1.",
                        details={"operation_id": operation_id},
                    )
                connection.execute(
                    """
                    INSERT INTO operations (
                        operation_id,
                        name,
                        status,
                        last_event_id,
                        created_at,
                        started_at,
                        finished_at,
                        is_finished
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        name,
                        status or "running",
                        event.event_id,
                        timestamp,
                        timestamp if event.type == EventType.OPERATION_STARTED else None,
                        timestamp if event.is_final else None,
                        int(event.is_final),
                    ),
                )
            else:
                if bool(existing["is_finished"]):
                    raise RavenError(
                        ErrorCode.EVENT_STREAM_FINISHED,
                        f"Operation '{operation_id}' is already finished.",
                    )
                expected_event_id = int(existing["last_event_id"]) + 1
                if event.event_id != expected_event_id:
                    raise RavenError(
                        ErrorCode.OPERATION_DATABASE_CORRUPTED,
                        "Event IDs must be contiguous within an operation.",
                        details={
                            "operation_id": operation_id,
                            "expected_event_id": expected_event_id,
                            "received_event_id": event.event_id,
                        },
                    )
                operation_name = (
                    name
                    if status is not None and name
                    else str(existing["name"])
                )
                connection.execute(
                    """
                    UPDATE operations
                    SET name = ?,
                        status = ?,
                        last_event_id = ?,
                        started_at = ?,
                        finished_at = ?,
                        is_finished = ?
                    WHERE operation_id = ?
                    """,
                    (
                        operation_name,
                        status or str(existing["status"]),
                        event.event_id,
                        (
                            timestamp
                            if event.type == EventType.OPERATION_STARTED
                            else existing["started_at"]
                        ),
                        timestamp if event.is_final else existing["finished_at"],
                        int(event.is_final),
                        operation_id,
                    ),
                )

            task_status = _TASK_STATUS_BY_EVENT.get(event.type)
            if event.task_id is not None and task_status is not None:
                task_error = event.data.get("error")
                error_json = (
                    json.dumps(task_error, separators=(",", ":"))
                    if isinstance(task_error, dict)
                    else None
                )
                cursor = connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?,
                        started_at = COALESCE(?, started_at),
                        finished_at = COALESCE(?, finished_at),
                        error_json = COALESCE(?, error_json)
                    WHERE task_id = ? AND operation_id = ?
                    """,
                    (
                        task_status,
                        timestamp if event.type == EventType.OPERATION_TASK_STARTED else None,
                        timestamp if event.type in _TERMINAL_TASK_EVENT_TYPES else None,
                        error_json,
                        str(event.task_id),
                        operation_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RavenError(
                        ErrorCode.OPERATION_DATABASE_CORRUPTED,
                        "A task lifecycle event references an unknown operation task.",
                        details={
                            "operation_id": operation_id,
                            "task_id": str(event.task_id),
                        },
                    )

            connection.execute(
                """
                INSERT INTO events (
                    operation_id,
                    event_id,
                    type,
                    timestamp,
                    task_id,
                    task_name,
                    is_final,
                    data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    event.event_id,
                    event.type.value,
                    timestamp,
                    serialized["task_id"],
                    event.task_name,
                    int(event.is_final),
                    json.dumps(serialized["data"], separators=(",", ":")),
                ),
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise


    def _read_metadata(
        self,
        operation_id: str,
    ) -> tuple[int, int, str | None] | None:
        row = self._require_connection().execute(
            """
            SELECT last_event_id, is_finished, finished_at
            FROM operations
            WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        return int(row["last_event_id"]), int(row["is_finished"]), row["finished_at"]


    def _read_after(
        self,
        operation_id: str,
        after_event_id: int,
    ) -> list[sqlite3.Row]:
        return self._require_connection().execute(
            """
            SELECT
                operation_id,
                event_id,
                type,
                timestamp,
                task_id,
                task_name,
                is_final,
                data_json
            FROM events
            WHERE operation_id = ? AND event_id > ?
            ORDER BY event_id
            """,
            (operation_id, after_event_id),
        ).fetchall()


    def _read_task(self, task_id: str) -> sqlite3.Row | None:
        return self._require_connection().execute(
            """
            SELECT
                task_id,
                operation_id,
                name,
                is_root,
                status,
                retry_policy,
                retry_input_json,
                attempt,
                retry_of_operation_id,
                retry_of_task_id,
                created_at,
                started_at,
                finished_at,
                error_json
            FROM tasks
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()


    def _read_retry(self, task_id: str) -> sqlite3.Row | None:
        return self._require_connection().execute(
            """
            SELECT
                task_id,
                operation_id,
                name,
                is_root,
                status,
                retry_policy,
                retry_input_json,
                attempt,
                retry_of_operation_id,
                retry_of_task_id,
                created_at,
                started_at,
                finished_at,
                error_json
            FROM tasks
            WHERE retry_of_task_id = ?
            """,
            (task_id,),
        ).fetchone()


    def _unfinished_tasks(self, operation_id: str) -> list[sqlite3.Row]:
        return self._require_connection().execute(
            """
            SELECT
                task_id,
                operation_id,
                name,
                is_root,
                status,
                retry_policy,
                retry_input_json,
                attempt,
                retry_of_operation_id,
                retry_of_task_id,
                created_at,
                started_at,
                finished_at,
                error_json
            FROM tasks
            WHERE operation_id = ?
              AND status NOT IN ('completed', 'failed', 'cancelled')
            ORDER BY created_at
            """,
            (operation_id,),
        ).fetchall()


    def _expired_operation_ids(self, cutoff: str) -> list[str]:
        rows = self._require_connection().execute(
            """
            SELECT operation_id
            FROM operations
            WHERE is_finished = 1 AND finished_at < ?
            ORDER BY finished_at
            """,
            (cutoff,),
        ).fetchall()
        return [str(row["operation_id"]) for row in rows]


    def _operation_ids(self) -> list[str]:
        rows = self._require_connection().execute(
            "SELECT operation_id FROM operations ORDER BY created_at"
        ).fetchall()
        return [str(row["operation_id"]) for row in rows]


    def _unfinished_operation_ids(self) -> list[str]:
        rows = self._require_connection().execute(
            """
            SELECT operation_id
            FROM operations
            WHERE is_finished = 0
            ORDER BY created_at
            """
        ).fetchall()
        return [str(row["operation_id"]) for row in rows]


    def _delete(self, operation_id: str) -> bool:
        connection = self._require_connection()
        cursor = connection.execute(
            "DELETE FROM operations WHERE operation_id = ?",
            (operation_id,),
        )
        return cursor.rowcount > 0


    def _checkpoint(self) -> None:
        row = self._require_connection().execute(
            "PRAGMA wal_checkpoint(FULL)"
        ).fetchone()
        if row is not None and int(row[0]) != 0:
            raise sqlite3.OperationalError("The WAL checkpoint remained busy")


    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Operation database is not open.")
        return self._connection


    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        return Event.model_validate(
            {
                "operation_id": row["operation_id"],
                "event_id": row["event_id"],
                "type": row["type"],
                "timestamp": row["timestamp"],
                "task_id": row["task_id"],
                "task_name": row["task_name"],
                "is_final": bool(row["is_final"]),
                "data": json.loads(row["data_json"]),
            }
        )


    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "operation_id": row["operation_id"],
            "name": row["name"],
            "is_root": bool(row["is_root"]),
            "status": row["status"],
            "retry_policy": row["retry_policy"],
            "retry_input": (
                json.loads(row["retry_input_json"])
                if row["retry_input_json"] is not None
                else None
            ),
            "attempt": row["attempt"],
            "retry_of_operation_id": row["retry_of_operation_id"],
            "retry_of_task_id": row["retry_of_task_id"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error": (
                json.loads(row["error_json"])
                if row["error_json"] is not None
                else None
            ),
        }


    @staticmethod
    def _database_error(message: str) -> RavenError:
        return RavenError(ErrorCode.OPERATION_DATABASE_FAILED, message)



class EventStream:
    """The persistent event stream belonging to one operation."""

    def __init__(
        self,
        operation_id: str,
        store: SQLiteOperationStore,
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
        """Make a registry-level synchronization failure visible to readers."""
        async with self._condition:
            if self._sync_error is None:
                self._sync_error = error
            self._condition.notify_all()


    async def delete(self) -> None:
        """Delete this operation's events and wake waiting readers."""
        async with self._condition:
            await self._load()
            await self._store.delete(self.operation_id)
            self._finished = True
            self._closed = True
            self._condition.notify_all()


    async def is_expired_before(self, timestamp: datetime) -> bool:
        """Return whether a finished stream predates ``timestamp``."""
        async with self._condition:
            await self._load()
            return (
                self._finished
                and self._finished_at is not None
                and self._finished_at < timestamp
            )


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



class EventStreamRegistry:
    """Own one user's event streams over a shared operation store."""

    def __init__(
        self,
        paths: PathConfig,
        *,
        sync_interval: float = OPERATION_SYNC_INTERVAL_SECONDS,
    ) -> None:
        if sync_interval <= 0:
            raise RavenError(
                ErrorCode.INVALID_OPERATION_SYNC_INTERVAL,
                "Operation synchronization interval must be greater than zero.",
            )
        self.storage_dir = paths.operation_storage_dir
        self.database_path = paths.operation_database_path
        self.sync_interval = sync_interval
        self._store = SQLiteOperationStore(self.database_path)
        self._streams: dict[str, EventStream] = {}
        self._sync_service = OperationSyncService(self, sync_interval)
        self._sync_error: RavenError | None = None
        self._closed = False


    @property
    def is_healthy(self) -> bool:
        return self._sync_error is None


    @property
    def operation_store(self) -> SQLiteOperationStore:
        return self._store


    async def start(self) -> None:
        """Open the operation database and start periodic checkpoints."""
        self._ensure_open()
        self._ensure_sync_healthy()
        await self._store.start()
        await self._sync_service.start()


    def get(self, operation_id: str) -> EventStream:
        """Return the stream for an operation, reopening it from SQLite if needed."""
        self._ensure_open()
        self._validate_operation_id(operation_id)
        stream = self._streams.get(operation_id)
        if stream is None:
            stream = EventStream(
                operation_id=operation_id,
                store=self._store,
                health_check=self._ensure_sync_healthy,
                sync_failure=self._record_sync_failure,
            )
            self._streams[operation_id] = stream
        return stream


    async def stored_operation_ids(self) -> list[str]:
        """Return operation UUIDs persisted in this user's operation database."""
        self._ensure_open()
        return await self._store.operation_ids()


    async def unfinished_operation_ids(self) -> list[str]:
        """Return operation UUIDs that do not have a terminal event."""
        self._ensure_open()
        return await self._store.unfinished_operation_ids()


    async def expired_operation_ids(self, cutoff: datetime) -> list[str]:
        """Return terminal operation IDs older than the retention cutoff."""
        self._ensure_open()
        return await self._store.expired_operation_ids(cutoff)


    async def delete(self, operation_id: str) -> None:
        stream = self.get(operation_id)
        await stream.delete()
        self._streams.pop(operation_id, None)


    async def sync_dirty(self) -> list[str]:
        """Checkpoint dirty operation data and return affected operation IDs."""
        self._ensure_open()
        self._ensure_sync_healthy()
        dirty_operation_ids = [
            stream.operation_id
            for stream in self._streams.values()
            if stream.is_dirty
        ]
        if not self._store.is_dirty:
            return []

        try:
            await self._store.checkpoint()
        except RavenError as exc:
            await self._record_sync_failure(exc)
            raise
        return dirty_operation_ids


    async def close(self) -> None:
        """Checkpoint and close this user's operation database and event streams."""
        if self._closed:
            return

        await self._sync_service.close()
        close_error: BaseException | None = None
        if self._store.is_dirty:
            try:
                await self._store.checkpoint()
            except BaseException as exc:
                close_error = exc

        await asyncio.gather(*(stream.close() for stream in self._streams.values()))
        await self._store.close()
        self._closed = True

        if close_error is not None:
            raise close_error


    def _ensure_open(self) -> None:
        if self._closed:
            raise RavenError(
                ErrorCode.EVENT_STREAM_CLOSED,
                "Event stream registry is closed.",
            )


    def _ensure_sync_healthy(self) -> None:
        if self._sync_error is not None:
            raise self._sync_error


    async def _record_sync_failure(
        self,
        error: RavenError,
        source_operation_id: str | None = None,
    ) -> None:
        if self._sync_error is None:
            self._sync_error = error
        active_error = self._sync_error or error
        await asyncio.gather(
            *(
                stream.mark_sync_failed(active_error)
                for stream in self._streams.values()
                if stream.operation_id != source_operation_id
            )
        )


    @staticmethod
    def _validate_operation_id(operation_id: str) -> None:
        try:
            parsed = UUID(operation_id)
        except (TypeError, ValueError) as exc:
            raise RavenError(
                ErrorCode.INVALID_OPERATION_ID,
                "operation_id must be a valid UUID.",
            ) from exc

        if operation_id not in {parsed.hex, str(parsed)}:
            raise RavenError(
                ErrorCode.INVALID_OPERATION_ID,
                "operation_id must use the canonical UUID format.",
            )



class OperationSyncService:
    """Periodically checkpoint one user's dirty operation database."""

    def __init__(self, registry: EventStreamRegistry, interval: float) -> None:
        self._registry = registry
        self.interval = interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False


    async def start(self) -> None:
        if self._closed:
            raise RavenError(
                ErrorCode.EVENT_STREAM_CLOSED,
                "Event synchronization service is closed.",
            )
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(),
            name="raven-operation-sync",
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
                await self._registry.sync_dirty()
            except RavenError:
                return



class OperationCleanupService:
    """Delete finished operations after a configured retention period."""

    def __init__(self, registry: EventStreamRegistry, retention: timedelta) -> None:
        if retention < timedelta(0):
            raise RavenError(
                ErrorCode.INVALID_RETENTION,
                "retention cannot be negative.",
            )
        self._registry = registry
        self.retention = retention


    async def run_once(self, now: datetime | None = None) -> list[str]:
        """Delete expired finished streams and return their operation IDs."""
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        cutoff = current_time.astimezone(timezone.utc) - self.retention
        operation_ids = await self._registry.expired_operation_ids(cutoff)

        for operation_id in operation_ids:
            await self._registry.delete(operation_id)
        return operation_ids
