"""Conversation-scoped storage for deliberate manual actions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from llama_index.core.llms import ChatMessage
from pydantic import BaseModel, ConfigDict

from ..core.async_utils import run_in_thread
from ..core.config import SQLITE_MAX_INTEGER
from ..core.errors import ErrorCode, RavenError


ActivityKind = Literal[
    "list_knowledges", "list_files", "list_sections",
    "retrieval", "open_file", "open_section", "reconstruction",
]
_SCHEMA_VERSION = 2
ACTIVITY_MESSAGE_KEY = "nraven_activity_message"


class Activity(BaseModel):
    """One completed manual action and its compact result references."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int
    activity_id: UUID
    operation_id: UUID
    kind: ActivityKind
    started_at: datetime
    recorded_at: datetime
    details: dict[str, Any]
    turn_id: UUID | None = None


class ActivityMessageEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    activity_id: UUID
    context_text: str


class ActivityMessage(BaseModel):
    """The user message assembled from captured activity and the current query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    activities: tuple[ActivityMessageEntry, ...]
    current_query: str

    @classmethod
    def from_message(cls, message: ChatMessage) -> "ActivityMessage | None":
        if message.additional_kwargs.get(ACTIVITY_MESSAGE_KEY) == 1:
            return cls.model_validate_json(message.content or "")
        return None


class ActivityLog:
    """Store actions in commit order without keeping an open SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def append(
        self,
        kind: ActivityKind,
        *,
        operation_id: UUID,
        started_at: datetime,
        details: dict[str, Any],
    ) -> Activity:
        return await run_in_thread(self._append, kind, operation_id, started_at, details)

    async def list(
        self, *, after_sequence: int = 0, limit: int = 50, turn_id: UUID | None = None,
    ) -> list[Activity]:
        if type(after_sequence) is not int or not 0 <= after_sequence <= SQLITE_MAX_INTEGER:
            raise RavenError(ErrorCode.INVALID_METADATA, "Invalid activity cursor.")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise RavenError(ErrorCode.INVALID_METADATA, "Activity limit must be between 1 and 200.")
        return await run_in_thread(self._list, after_sequence, limit, turn_id=turn_id)

    async def pending(self, limit: int) -> list[Activity]:
        return await run_in_thread(self._list, 0, limit, pending_only=True)

    def pending_ids(self) -> set[UUID]:
        if not self.path.is_file():
            return set()
        with closing(sqlite3.connect(self.path)) as connection:
            return {
                UUID(activity_id)
                for (activity_id,) in connection.execute(
                    "SELECT activity_id FROM activities WHERE turn_id IS NULL"
                )
            }

    def prepare(self) -> None:
        """Ensure the schema is ready before recovering activity links."""
        if self.path.is_file():
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ensure_schema(connection)

    def link(self, turn_id: str, activity_ids: Sequence[UUID]) -> None:
        if not activity_ids:
            return
        if not self.path.is_file():
            raise RavenError(ErrorCode.PERSISTENCE_FAILED, "The turn's activity database is missing.")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            for activity_id in activity_ids:
                updated = connection.execute(
                    """
                    UPDATE activities SET turn_id = ?
                    WHERE activity_id = ? AND (turn_id IS NULL OR turn_id = ?)
                    """,
                    (turn_id, str(activity_id), turn_id),
                )
                if updated.rowcount != 1:
                    raise RavenError(
                        ErrorCode.PERSISTENCE_FAILED,
                        f"Activity '{activity_id}' could not be linked to turn '{turn_id}'.",
                    )

    def link_legacy_range(self, turn_id: str, after_sequence: int, through_sequence: int) -> None:
        if not self.path.is_file():
            return
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """
                UPDATE activities SET turn_id = ?
                WHERE turn_id IS NULL AND sequence > ? AND sequence <= ?
                """,
                (turn_id, after_sequence, through_sequence),
            )

    def remove(self, activity_id: UUID) -> bool:
        if not self.path.is_file():
            return False
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT turn_id FROM activities WHERE activity_id = ?", (str(activity_id),),
            ).fetchone()
            if row is None:
                return False
            if row[0] is not None:
                raise RavenError(ErrorCode.ACTIVITY_COMMITTED, f"Activity '{activity_id}' is linked to a turn.")
            connection.execute("DELETE FROM activities WHERE activity_id = ?", (str(activity_id),))
            return True

    def _append(
        self,
        kind: ActivityKind,
        operation_id: UUID,
        started_at: datetime,
        details: dict[str, Any],
    ) -> Activity:
        activity_id = uuid4()
        payload = json.dumps(details, ensure_ascii=False)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)
            recorded_at = datetime.now(timezone.utc)
            row = connection.execute(
                """
                INSERT INTO activities (
                    activity_id, operation_id, kind, started_at, recorded_at, details_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(activity_id), str(operation_id), kind, started_at.isoformat(),
                 recorded_at.isoformat(), payload),
            )
            sequence = row.lastrowid
            assert sequence is not None
            activity = Activity(
                sequence=sequence,
                activity_id=activity_id,
                operation_id=operation_id,
                kind=kind,
                started_at=started_at,
                recorded_at=recorded_at,
                details=details,
            )
        return activity

    def _list(
        self, after_sequence: int, limit: int, *,
        turn_id: UUID | None = None, pending_only: bool = False,
    ) -> list[Activity]:
        if not self.path.is_file():
            return []
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            if self._check_version(connection) == 0:
                return []
            conditions = ["sequence > ?"]
            parameters: list[int | str] = [after_sequence]
            if pending_only:
                conditions.append("turn_id IS NULL")
            elif turn_id is not None:
                conditions.append("turn_id = ?")
                parameters.append(str(turn_id))
            parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT sequence, activity_id, operation_id, kind, started_at, recorded_at, details_json, turn_id
                FROM activities WHERE {" AND ".join(conditions)} ORDER BY sequence LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [
            Activity(
                sequence=sequence, activity_id=activity_id, operation_id=operation_id,
                kind=kind, started_at=started_at, recorded_at=recorded_at,
                details=json.loads(details_json),
                turn_id=turn_id,
            )
            for sequence, activity_id, operation_id, kind, started_at, recorded_at, details_json, turn_id in rows
        ]

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        version = self._check_version(connection)
        if version == _SCHEMA_VERSION:
            return
        if version == 1:
            connection.execute("ALTER TABLE activities ADD COLUMN turn_id TEXT")
        else:
            connection.execute(
                """
                CREATE TABLE activities (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    activity_id TEXT NOT NULL UNIQUE,
                    operation_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    turn_id TEXT
                )
                """
            )
        connection.execute("CREATE INDEX activities_turn_sequence ON activities(turn_id, sequence)")
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @staticmethod
    def _check_version(connection: sqlite3.Connection) -> int:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, 1, _SCHEMA_VERSION):
            raise RavenError(
                ErrorCode.UNSUPPORTED_METADATA_VERSION,
                "Unsupported activity database version.",
                details={"schema_version": version},
            )
        return version
