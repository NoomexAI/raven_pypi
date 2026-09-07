"""SQLite storage for committed conversation turns and model context."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llama_index.core.llms import ChatMessage

from ..core.errors import ErrorCode, RavenError

SCHEMA_VERSION = 1


class ConversationMessageStore:
    """Persist canonical turns separately from compacted model context."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()


    def open(self) -> None:
        """Open the database and initialize its schema."""
        with self._lock:
            if self._connection is not None:
                return

            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                timeout=5.0,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row

            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA busy_timeout = 5000")

                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, SCHEMA_VERSION}:
                    raise RavenError(
                        ErrorCode.UNSUPPORTED_METADATA_VERSION,
                        f"Unsupported message database version for '{self.path.parent.name}'.",
                        details={"schema_version": version},
                    )

                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS turns (
                        turn_id TEXT PRIMARY KEY,
                        operation_id TEXT,
                        user_query TEXT NOT NULL,
                        result_json TEXT,
                        memory_indexed INTEGER NOT NULL DEFAULT 0,
                        committed_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS messages (
                        message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        turn_id TEXT NOT NULL,
                        message_order INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        message_json TEXT NOT NULL,
                        FOREIGN KEY (turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE,
                        UNIQUE (turn_id, message_order)
                    );

                    CREATE INDEX IF NOT EXISTS idx_messages_turn_order
                    ON messages(turn_id, message_order);

                    CREATE TABLE IF NOT EXISTS context_messages (
                        position INTEGER PRIMARY KEY,
                        role TEXT NOT NULL,
                        message_json TEXT NOT NULL
                    );
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            except Exception:
                connection.close()
                raise

            self._connection = connection


    def close(self) -> None:
        """Checkpoint pending WAL pages and close the database."""
        with self._lock:
            connection = self._connection
            if connection is None:
                return

            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
            self._connection = None


    def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._require_connection().execute(
                "SELECT * FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        return None if row is None else self._turn_from_row(row)


    def list_messages(self) -> list[ChatMessage]:
        """Return the immutable, complete transcript in commit order."""
        with self._lock:
            rows = self._require_connection().execute(
                """
                SELECT m.message_json
                FROM messages AS m
                JOIN turns AS t ON t.turn_id = m.turn_id
                ORDER BY t.rowid, m.message_order
                """
            ).fetchall()
        return [self._message_from_json(row["message_json"]) for row in rows]


    def get_turn_messages(self, turn_id: str) -> list[ChatMessage]:
        with self._lock:
            rows = self._require_connection().execute(
                """
                SELECT message_json
                FROM messages
                WHERE turn_id = ?
                ORDER BY message_order
                """,
                (turn_id,),
            ).fetchall()
        return [self._message_from_json(row["message_json"]) for row in rows]


    def get_context_messages(self) -> list[ChatMessage]:
        with self._lock:
            rows = self._require_connection().execute(
                """
                SELECT message_json
                FROM context_messages
                ORDER BY position
                """
            ).fetchall()
        return [self._message_from_json(row["message_json"]) for row in rows]


    def commit_turn(
        self,
        *,
        turn_id: str,
        operation_id: str | None,
        user_query: str,
        messages: Sequence[ChatMessage],
        result: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically commit one turn and append it to active context."""
        serialized_messages = [self._message_values(message) for message in messages]
        result_json = (
            json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            if result is not None
            else None
        )
        committed_at = datetime.now(timezone.utc).isoformat()

        with self._lock:
            connection = self._require_connection()
            existing = connection.execute(
                "SELECT * FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if existing is not None:
                record = self._turn_from_row(existing)
                if record["user_query"] != user_query:
                    raise RavenError(
                        ErrorCode.CONVERSATION_TURN_CONFLICT,
                        f"Turn '{turn_id}' is already assigned to a different query.",
                    )
                return record, False

            with connection:
                connection.execute(
                    """
                    INSERT INTO turns (
                        turn_id, operation_id, user_query, result_json,
                        memory_indexed, committed_at
                    ) VALUES (?, ?, ?, ?, 0, ?)
                    """,
                    (
                        turn_id,
                        operation_id,
                        user_query,
                        result_json,
                        committed_at,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO messages (
                        turn_id, message_order, role, message_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (turn_id, index, role, message_json)
                        for index, (role, message_json) in enumerate(
                            serialized_messages
                        )
                    ],
                )
                next_position = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(position) + 1, 0) FROM context_messages"
                    ).fetchone()[0]
                )
                connection.executemany(
                    """
                    INSERT INTO context_messages (position, role, message_json)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (next_position + index, role, message_json)
                        for index, (role, message_json) in enumerate(
                            serialized_messages
                        )
                    ],
                )

            record = self.get_turn(turn_id)
            if record is None:
                raise RavenError(
                    ErrorCode.PERSISTENCE_FAILED,
                    f"Turn '{turn_id}' was not available after commit.",
                )
            return record, True


    def replace_context(self, messages: Sequence[ChatMessage]) -> None:
        """Atomically replace only the model-facing compacted context."""
        serialized_messages = [self._message_values(message) for message in messages]
        with self._lock:
            connection = self._require_connection()
            with connection:
                connection.execute("DELETE FROM context_messages")
                connection.executemany(
                    """
                    INSERT INTO context_messages (position, role, message_json)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (position, role, message_json)
                        for position, (role, message_json) in enumerate(
                            serialized_messages
                        )
                    ],
                )


    def list_unindexed_turn_ids(self) -> list[str]:
        with self._lock:
            rows = self._require_connection().execute(
                """
                SELECT turn_id
                FROM turns
                WHERE memory_indexed = 0
                ORDER BY rowid
                """
            ).fetchall()
        return [str(row["turn_id"]) for row in rows]


    def mark_memory_indexed(self, turn_id: str) -> None:
        with self._lock:
            connection = self._require_connection()
            with connection:
                cursor = connection.execute(
                    "UPDATE turns SET memory_indexed = 1 WHERE turn_id = ?",
                    (turn_id,),
                )
            if cursor.rowcount != 1:
                raise RavenError(
                    ErrorCode.CONVERSATION_TURN_NOT_FOUND,
                    f"Turn '{turn_id}' does not exist.",
                )


    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RavenError(
                ErrorCode.CONVERSATION_NOT_STARTED,
                f"Message database '{self.path}' is not open.",
            )
        return self._connection


    @staticmethod
    def _message_values(message: ChatMessage) -> tuple[str, str]:
        return (
            message.role.value,
            json.dumps(
                message.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )


    @staticmethod
    def _message_from_json(value: str) -> ChatMessage:
        return ChatMessage.model_validate(json.loads(value))


    @staticmethod
    def _turn_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result_json = row["result_json"]
        return {
            "turn_id": str(row["turn_id"]),
            "operation_id": (
                str(row["operation_id"])
                if row["operation_id"] is not None
                else None
            ),
            "user_query": str(row["user_query"]),
            "result": json.loads(result_json) if result_json is not None else None,
            "memory_indexed": bool(row["memory_indexed"]),
            "committed_at": str(row["committed_at"]),
        }
