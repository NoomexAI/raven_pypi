"""SQLite persistence for Raven operations, tasks, and events."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import ErrorCode, RavenError
from .events import Event, EventType


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



class SQLiteOperationStore:
    """Persist one user's operations, tasks, and events in SQLite."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="raven-operation-store",
        )
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
                await self._run_in_store_thread(self._open_connection)
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
                await self._run_in_store_thread(self._append, event)
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
                await self._run_in_store_thread(
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
                row = await self._run_in_store_thread(self._read_task, task_id)
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
                row = await self._run_in_store_thread(self._read_retry, task_id)
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
                rows = await self._run_in_store_thread(
                    self._unfinished_tasks,
                    operation_id,
                )
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
                row = await self._run_in_store_thread(
                    self._read_metadata,
                    operation_id,
                )
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


    async def read_after(
        self,
        operation_id: str,
        after_event_id: int,
        *,
        limit: int | None = None,
    ) -> list[Event]:
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise RavenError(
                ErrorCode.INVALID_EVENT_PAGE_SIZE,
                "Event page size must be a positive integer.",
            )

        await self.start()
        async with self._lock:
            try:
                rows = await self._run_in_store_thread(
                    self._read_after,
                    operation_id,
                    after_event_id,
                    limit,
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
                return await self._run_in_store_thread(self._operation_ids)
            except Exception as exc:
                raise self._database_error("Operation IDs could not be read.") from exc


    async def unfinished_operation_ids(self) -> list[str]:
        await self.start()
        async with self._lock:
            try:
                return await self._run_in_store_thread(
                    self._unfinished_operation_ids
                )
            except Exception as exc:
                raise self._database_error(
                    "Unfinished operation IDs could not be read."
                ) from exc


    async def expired_operation_ids(self, cutoff: datetime) -> list[str]:
        await self.start()
        async with self._lock:
            try:
                return await self._run_in_store_thread(
                    self._expired_operation_ids,
                    cutoff.isoformat(),
                )
            except Exception as exc:
                raise self._database_error("Expired operations could not be read.") from exc


    async def delete(self, operation_id: str) -> None:
        await self.start()
        async with self._lock:
            try:
                changed = await self._run_in_store_thread(self._delete, operation_id)
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
                await self._run_in_store_thread(self._checkpoint)
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
            try:
                if connection is not None:
                    await self._run_in_store_thread(connection.close)
            finally:
                self._executor.shutdown(wait=False)


    async def _run_in_store_thread(self, function: Any, *args: Any) -> Any:
        """Run one database call without releasing ownership before it exits."""
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, partial(function, *args))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.shield(future)
            raise


    def _open_connection(self) -> None:
        if self._connection is None:
            self._connection = self._open()


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
        limit: int | None,
    ) -> list[sqlite3.Row]:
        query = """
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
        """
        parameters: tuple[Any, ...] = (operation_id, after_event_id)
        if limit is not None:
            query += " LIMIT ?"
            parameters += (limit,)
        return self._require_connection().execute(query, parameters).fetchall()


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
