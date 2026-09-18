"""Conversation persistence and lifecycle management for Raven."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shutil
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID, uuid4

from llama_index.core.base.llms.types import MessageRole
from llama_index.core.llms import ChatMessage
from llama_index.core.memory import ChatSummaryMemoryBuffer, VectorMemory
from llama_index.core.schema import TextNode
from llama_index.core.storage.chat_store import SimpleChatStore
from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models

from ..core.async_utils import await_completion, run_in_thread
from ..core.config import (
    DEFAULT_OPEN_CONVERSATION_LIMIT,
    SQLITE_MAX_INTEGER,
    PathConfig,
)
from ..core.errors import ErrorCode, RavenError, error_payload
from ..core.events import Event, EventType
from ..core.operations import Operation, OperationManager, OperationTask, OperationType
from ..providers.model_identity import embedding_identity
from .discovery import DiscoveryIssue
from .knowledge_base import KnowledgeBase


_ResultT = TypeVar("_ResultT")


DEFAULT_TITLE = "New Conversation"
TITLE_SYSTEM_PROMPT = (
    "Create a concise title for this conversation from the user's first message. "
    "Return only the title, without quotation marks, labels, or explanation."
)
MEMORY_SUMMARY_PROMPT = (
    "Compact the conversation into a concise memory for continuing it later. "
    "Preserve exact user-provided facts, names, identifiers, numbers, decisions, "
    "constraints, corrections, commitments, and unresolved requests. Preserve "
    "important facts learned from tool results when they affected the answer. "
    "Distinguish user statements from assistant claims. Omit routine greetings, "
    "reasoning traces, and redundant wording. Do not invent or infer missing facts."
)
PERSISTENCE_VERSION = 1
PREFERENCE_SCHEMA_VERSION = 1
MEMORY_COLLECTION = "messages"
MEMORY_VECTOR_NAME = "text-dense"
_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CONVERSATION_STORE_SCHEMA_VERSION = 1


class _ConversationMessageStore:
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
                if version not in {0, _CONVERSATION_STORE_SCHEMA_VERSION}:
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

                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    f"PRAGMA user_version = {_CONVERSATION_STORE_SCHEMA_VERSION}"
                )
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


    def list_messages_page(
        self,
        limit: int,
        after_message_id: int = 0,
    ) -> list[dict[str, Any]]:
        """Read one canonical transcript page by durable message ID."""
        with self._lock:
            rows = self._require_connection().execute(
                """
                SELECT message_id, turn_id, message_order, message_json
                FROM messages
                WHERE message_id > ?
                ORDER BY message_id
                LIMIT ?
                """,
                (after_message_id, limit),
            ).fetchall()
        return [
            {
                "message_id": int(row["message_id"]),
                "turn_id": str(row["turn_id"]),
                "message_order": int(row["message_order"]),
                "message": self._message_from_json(row["message_json"]),
            }
            for row in rows
        ]


    def get_turn_messages(self, turn_id: str) -> list[ChatMessage]:
        """Return the canonical messages belonging to one committed turn."""
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


    def list_turn_messages_page(
        self,
        turn_id: str,
        limit: int,
        after_message_id: int = 0,
    ) -> list[dict[str, Any]]:
        """Read one committed turn's transcript without loading other turns."""
        with self._lock:
            rows = self._require_connection().execute(
                """
                SELECT message_id, turn_id, message_order, message_json
                FROM messages
                WHERE turn_id = ? AND message_id > ?
                ORDER BY message_id
                LIMIT ?
                """,
                (turn_id, after_message_id, limit),
            ).fetchall()
        return [
            {
                "message_id": int(row["message_id"]),
                "turn_id": str(row["turn_id"]),
                "message_order": int(row["message_order"]),
                "message": self._message_from_json(row["message_json"]),
            }
            for row in rows
        ]


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


    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._require_connection().execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
        return None if row is None else str(row["value"])


    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            connection = self._require_connection()
            with connection:
                connection.execute(
                    """
                    INSERT INTO settings (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
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




class Conversation:
    """Durable identity and metadata for one conversation."""

    def __init__(
        self,
        dir_path: Path,
        *,
        operation_manager: OperationManager,
        before_open: Callable[["Conversation"], Awaitable[None]] | None = None,
        after_open: Callable[["Conversation"], Awaitable[None]] | None = None,
    ) -> None:
        self.dir_path = Path(dir_path)
        self.conversation_id = ""
        self.knowledge_name: str | None = None
        self.title = DEFAULT_TITLE
        self.is_titled = False
        self.pinned = False
        self.created_at: datetime | None = None

        self.metadata_path = self.dir_path / "metadata.json"
        self.messages_path = self.dir_path / "messages.sqlite3"
        self.preferences_path = self.dir_path / "preferences.json"
        self.qdrant_dir = self.dir_path / "qdrant"

        self._qdrant: QdrantClient | None = None
        self._pending_qdrant_close: QdrantClient | None = None
        self._storage_cleanup_pending = False
        self._message_store = _ConversationMessageStore(self.messages_path)
        self._chat_store: SimpleChatStore | None = None
        self._chat_memory: ChatSummaryMemoryBuffer | None = None
        self._vector_memory: VectorMemory | None = None
        self._preferences: list[dict[str, str]] = []
        self._start_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._memory_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._title_lock = asyncio.Lock()
        self._active_turn_owner: UUID | None = None
        self._memory_configuration: tuple[int, int, int, int] | None = None
        self._started = False
        self._closed = False
        self._operation_manager = operation_manager
        self._before_open = before_open
        self._after_open = after_open
        self._usage_lock = threading.Lock()
        self._active_uses = 0


    @property
    def type(self) -> str:
        """Return the conversation scope derived from its knowledge binding."""
        return "local" if self.knowledge_name else "global"


    def _set_metadata(
        self,
        *,
        conversation_id: str,
        knowledge_name: str | None,
        title: str,
        is_titled: bool,
        pinned: bool,
        created_at: datetime,
    ) -> None:
        """Set the complete conversation metadata state."""
        self.conversation_id = conversation_id
        self.knowledge_name = knowledge_name
        self.title = title
        self.is_titled = is_titled
        self.pinned = pinned
        self.created_at = created_at


    @property
    def is_started(self) -> bool:
        return self._started and not self._closed


    @property
    def has_active_turn(self) -> bool:
        return self._active_turn_owner is not None


    @property
    def active_uses(self) -> int:
        with self._usage_lock:
            return self._active_uses


    async def start(self) -> None:
        """Open the conversation storage without initializing model memory."""
        async with self._start_lock:
            if self.is_started:
                return
            if self._closed:
                raise RavenError(
                    ErrorCode.CONVERSATION_CLOSED,
                    f"Conversation '{self.conversation_id}' is closed.",
                )

            reserved = False
            try:
                if self._before_open is not None:
                    await self._before_open(self)
                    reserved = True

                async with self._lifecycle_lock:
                    if self.is_started:
                        return
                    if self._closed:
                        raise RavenError(
                            ErrorCode.CONVERSATION_CLOSED,
                            f"Conversation '{self.conversation_id}' is closed.",
                        )
                    await self._open_storage_safely()
                    self._started = True
            finally:
                if reserved and self._after_open is not None:
                    await self._after_open(self)


    async def close(self) -> None:
        """Close the conversation Qdrant database and memory resources."""
        async with self._lifecycle_lock:
            self._chat_store = None
            self._chat_memory = None
            self._vector_memory = None
            self._memory_configuration = None
            self._closed = True
            self._started = False
            await run_in_thread(self._close_open_storage)


    async def release_resources(self) -> bool:
        """Close idle storage while keeping this conversation reopenable."""
        async with self._lifecycle_lock:
            if not self.is_started:
                return True
            with self._usage_lock:
                if self.has_active_turn or self._active_uses:
                    return False
                self._chat_store = None
                self._chat_memory = None
                self._vector_memory = None
                self._memory_configuration = None
                self._started = False
            await run_in_thread(self._close_open_storage)
            return True


    async def _run_in_use(
        self,
        worker: Callable[[], Awaitable[_ResultT]],
    ) -> _ResultT:
        async with self._lifecycle_lock:
            if self._closed:
                raise RavenError(
                    ErrorCode.CONVERSATION_CLOSED,
                    f"Conversation '{self.conversation_id}' is closed.",
                )
            with self._usage_lock:
                self._active_uses += 1
        try:
            await self.start()
            return await worker()
        finally:
            with self._usage_lock:
                self._active_uses -= 1


    async def _claim_turn(self, session_id: UUID) -> None:
        """Reserve this conversation for one active turn."""
        async with self._turn_lock:
            if self._active_turn_owner is not None:
                raise RavenError(
                    ErrorCode.CONVERSATION_TURN_ACTIVE,
                    f"Conversation '{self.conversation_id}' already has an active turn.",
                )
            self._active_turn_owner = session_id


    async def _release_turn(self, session_id: UUID) -> None:
        """Release the active-turn reservation owned by ``session_id``."""
        async with self._turn_lock:
            if self._active_turn_owner == session_id:
                self._active_turn_owner = None


    async def initialize_memory(
        self,
        llm: Any,
        embed_model: Any,
        *,
        token_limit: int,
        memory_top_k: int = 5,
    ) -> None:
        """Initialize compacted chat memory and vector memory for this conversation."""
        if token_limit <= 0:
            raise ValueError("token_limit must be positive")
        if memory_top_k <= 0:
            raise ValueError("memory_top_k must be positive")
        if llm is None:
            raise RavenError(
                ErrorCode.LLM_MODEL_REQUIRED,
                "An LLM is required for compacted conversation memory.",
            )
        if embed_model is None:
            raise RavenError(
                ErrorCode.EMBEDDING_MODEL_REQUIRED,
                "An embedding model is required for vector conversation memory.",
            )

        await self.start()
        async with self._memory_lock:
            configuration = (
                id(llm),
                id(embed_model),
                token_limit,
                memory_top_k,
            )
            if (
                self._chat_memory is not None
                and self._memory_configuration == configuration
            ):
                return

            if self._chat_memory is not None:
                stale_qdrant = self._qdrant
                self._qdrant = None
                self._chat_store = None
                self._chat_memory = None
                self._vector_memory = None
                self._memory_configuration = None
                if stale_qdrant is not None:
                    await run_in_thread(stale_qdrant.close)

            if self._qdrant is None:
                await run_in_thread(self._open_qdrant)
            chat_store = await run_in_thread(self._load_chat_store)
            await run_in_thread(
                self._ensure_embedding_identity,
                embed_model,
            )
            await run_in_thread(self._ensure_memory_collection, embed_model)
            qdrant = self._require_qdrant()
            vector_store = QdrantVectorStore(
                collection_name=MEMORY_COLLECTION,
                client=qdrant,
                dense_vector_name=MEMORY_VECTOR_NAME,
            )
            chat_memory = ChatSummaryMemoryBuffer.from_defaults(
                llm=llm,
                chat_store=chat_store,
                chat_store_key="messages",
                token_limit=token_limit,
                summarize_prompt=MEMORY_SUMMARY_PROMPT,
                count_initial_tokens=True,
            )
            vector_memory = VectorMemory.from_defaults(
                vector_store=vector_store,
                embed_model=embed_model,
                retriever_kwargs={"similarity_top_k": memory_top_k},
            )

            self._chat_store = chat_store
            self._chat_memory = chat_memory
            self._vector_memory = vector_memory
            self._memory_configuration = configuration

        try:
            pending_turn_ids = await run_in_thread(
                self._message_store.list_unindexed_turn_ids
            )
            for turn_id in pending_turn_ids:
                await self._ensure_turn_indexed(turn_id)
        except BaseException:
            async with self._memory_lock:
                self._chat_store = None
                self._chat_memory = None
                self._vector_memory = None
                self._memory_configuration = None
            raise


    async def release_memory_resources(self) -> None:
        """Release model-bound memory adapters while keeping storage reopenable."""
        async with self._memory_lock:
            qdrant = self._qdrant
            self._qdrant = None
            self._chat_store = None
            self._chat_memory = None
            self._vector_memory = None
            self._memory_configuration = None
        if qdrant is not None:
            await run_in_thread(qdrant.close)


    async def validate_embedding_model(self, embed_model: Any) -> None:
        """Reject an adapter that does not match persisted vector memory."""
        await self.start()
        await run_in_thread(self._ensure_embedding_identity, embed_model)


    async def get_context_messages(
        self,
        *,
        initial_token_count: int = 0,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Start a context-loading task for this conversation."""
        active_operation = operation or await self._operation_manager.create(
            OperationType.CONVERSATION_GET_CONTEXT
        )
        return await active_operation.run(
            OperationType.CONVERSATION_GET_CONTEXT,
            lambda active_operation: self._run_in_use(
                lambda: self._get_context_messages(
                    initial_token_count=initial_token_count,
                    operation=active_operation,
                )
            ),
        )


    async def _get_context_messages(
        self,
        *,
        initial_token_count: int,
        operation: Operation,
    ) -> list[ChatMessage]:
        """Load context-fit messages and compact older history when needed."""
        memory = self._require_chat_memory()
        if memory.count_initial_tokens and initial_token_count >= memory.token_limit:
            raise RavenError(
                ErrorCode.CONTEXT_TOKEN_LIMIT_TOO_SMALL,
                "The conversation memory limit cannot fit the system prompt.",
                details={
                    "initial_token_count": initial_token_count,
                    "memory_token_limit": memory.token_limit,
                },
            )
        async with self._mutation_lock, self._memory_lock:
            messages_before = await run_in_thread(memory.get_all)
            message_count_before = len(messages_before)
            compaction_needed = await run_in_thread(
                self._memory_compaction_needed,
                memory,
                messages_before,
                initial_token_count,
            )

            if compaction_needed and operation is not None:
                await operation.publish(
                    Event(
                        type=EventType.CONVERSATION_MEMORY_COMPACTION_STARTED,
                        data={
                            "conversation_id": self.conversation_id,
                            "message_count": message_count_before,
                        },
                    )
                )

            try:
                messages = await memory.aget(initial_token_count=initial_token_count)
                await run_in_thread(
                    self._message_store.replace_context,
                    messages,
                )
            except Exception as exc:
                if compaction_needed and operation is not None:
                    await operation.publish(
                        Event(
                            type=EventType.CONVERSATION_MEMORY_COMPACTION_FAILED,
                            data={
                                "conversation_id": self.conversation_id,
                                "message_count": message_count_before,
                                "error": error_payload(exc),
                            },
                        )
                    )
                raise

            if compaction_needed and operation is not None:
                await operation.publish(
                    Event(
                        type=EventType.CONVERSATION_MEMORY_COMPACTION_COMPLETED,
                        data={
                            "conversation_id": self.conversation_id,
                            "messages_before": message_count_before,
                            "messages_after": len(messages),
                            "summarized_message_count": max(
                                0,
                                message_count_before - len(messages) + 1,
                            ),
                        },
                    )
                )
            return messages


    @staticmethod
    def _memory_compaction_needed(
        memory: ChatSummaryMemoryBuffer,
        messages: list[ChatMessage],
        initial_token_count: int,
    ) -> bool:
        """Estimate whether ChatSummaryMemoryBuffer will compact history."""
        if not messages:
            return False

        initial_tokens = initial_token_count if memory.count_initial_tokens else 0
        history_text = " ".join(str(message.content) for message in messages)
        history_tokens = len(memory.tokenizer_fn(history_text))
        return initial_tokens + history_tokens > memory.token_limit


    async def get_messages(self) -> list[ChatMessage]:
        """Return the complete stored chat history without applying a window."""
        return await self._run_in_use(
            lambda: run_in_thread(self._message_store.list_messages)
        )


    async def get_messages_page(
        self,
        limit: int,
        after_message_id: int = 0,
    ) -> list[dict[str, Any]]:
        """Return a bounded page of canonical, uncompacted messages."""
        self._validate_message_cursor(after_message_id)
        return await self._run_in_use(
            lambda: run_in_thread(
                self._message_store.list_messages_page,
                limit,
                after_message_id,
            )
        )


    async def get_turn_messages(
        self,
        turn_id: UUID | str,
    ) -> list[ChatMessage]:
        """Return only the canonical messages committed under ``turn_id``."""
        async def load_turn() -> list[ChatMessage]:
            normalized_turn_id = self._validate_turn_id(turn_id)
            turn = await run_in_thread(
                self._message_store.get_turn,
                normalized_turn_id,
            )
            if turn is None:
                raise RavenError(
                    ErrorCode.CONVERSATION_TURN_NOT_FOUND,
                    f"Turn '{normalized_turn_id}' does not exist.",
                )
            return await run_in_thread(
                self._message_store.get_turn_messages,
                normalized_turn_id,
            )

        return await self._run_in_use(load_turn)


    async def get_turn_messages_page(
        self,
        turn_id: UUID | str,
        limit: int,
        after_message_id: int = 0,
    ) -> list[dict[str, Any]]:
        """Return a bounded page after checking that the turn exists."""
        self._validate_message_cursor(after_message_id)

        async def load_page() -> list[dict[str, Any]]:
            normalized = self._validate_turn_id(turn_id)
            turn = await run_in_thread(self._message_store.get_turn, normalized)
            if turn is None:
                raise RavenError(
                    ErrorCode.CONVERSATION_TURN_NOT_FOUND,
                    f"Turn '{normalized}' does not exist.",
                )
            return await run_in_thread(
                self._message_store.list_turn_messages_page,
                normalized,
                limit,
                after_message_id,
            )

        return await self._run_in_use(load_page)


    async def search_memory(self, query: str) -> list[ChatMessage]:
        """Search semantic conversation memory for messages relevant to ``query``."""
        value = query.strip() if isinstance(query, str) else ""
        if not value:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "Memory query must be a non-empty string.",
            )

        vector_memory = self._require_vector_memory()
        async with self._memory_lock:
            return await vector_memory.aget(value)


    async def add_message(
        self,
        message: ChatMessage,
        *,
        index_in_vector_memory: bool = True,
    ) -> None:
        """Persist one standalone message as its own committed turn."""
        task = await self.append_turn(
            [message],
            turn_id=uuid4(),
            user_query=str(message.content or ""),
            index_in_vector_memory=index_in_vector_memory,
        )
        await task.result()


    async def append_turn(
        self,
        messages: Sequence[ChatMessage],
        *,
        turn_id: UUID | str | None = None,
        user_query: str | None = None,
        result: dict[str, Any] | None = None,
        index_in_vector_memory: bool = True,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Start an atomic, idempotent turn commit."""
        if not messages:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "A conversation turn must contain at least one message.",
            )
        normalized_turn_id = self._validate_turn_id(turn_id or uuid4())
        normalized_query = self._turn_query(messages, user_query)
        active_operation = operation or await self._operation_manager.create(
            OperationType.CONVERSATION_APPEND_TURN
        )
        return await active_operation.run(
            OperationType.CONVERSATION_APPEND_TURN,
            lambda active_operation: self._run_in_use(
                lambda: self._append_turn(
                    messages,
                    turn_id=normalized_turn_id,
                    user_query=normalized_query,
                    result=result,
                    index_in_vector_memory=index_in_vector_memory,
                    operation=active_operation,
                )
            ),
        )


    async def _append_turn(
        self,
        messages: Sequence[ChatMessage],
        *,
        turn_id: str,
        user_query: str,
        result: dict[str, Any] | None,
        index_in_vector_memory: bool,
        operation: Operation,
    ) -> dict[str, Any]:
        """Commit a complete turn and reconcile its derived memory index."""
        self._require_chat_memory()
        self._require_vector_memory()
        await self._emit(
            operation,
            EventType.CONVERSATION_TURN_COMMIT_STARTED,
            {
                "conversation_id": self.conversation_id,
                "turn_id": turn_id,
            },
        )

        try:
            async with self._mutation_lock:
                operation.raise_if_cancelled()
                record, created = await run_in_thread(
                    self._message_store.commit_turn,
                    turn_id=turn_id,
                    operation_id=str(operation.operation_id),
                    user_query=user_query,
                    messages=messages,
                    result=result,
                )

                await self._reload_context_memory()
                if index_in_vector_memory:
                    await self._ensure_turn_indexed(turn_id, operation=operation)
                elif not record["memory_indexed"]:
                    await run_in_thread(
                        self._message_store.mark_memory_indexed,
                        turn_id,
                    )

            event_type = (
                EventType.CONVERSATION_TURN_COMMITTED
                if created
                else EventType.CONVERSATION_TURN_REUSED
            )
            await self._emit(
                operation,
                event_type,
                {
                    "conversation_id": self.conversation_id,
                    "turn_id": turn_id,
                    "message_count": len(messages),
                },
            )
            updated = await run_in_thread(self._message_store.get_turn, turn_id)
            if updated is None:
                raise RavenError(
                    ErrorCode.CONVERSATION_TURN_NOT_FOUND,
                    f"Turn '{turn_id}' does not exist after commit.",
                )
            return updated
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                operation,
                EventType.CONVERSATION_TURN_COMMIT_FAILED,
                {
                    "conversation_id": self.conversation_id,
                    "turn_id": turn_id,
                    "error": error_payload(exc),
                },
            )
            raise


    async def get_turn(self, turn_id: UUID | str) -> dict[str, Any] | None:
        """Return one committed turn by its stable identity."""
        normalized_turn_id = self._validate_turn_id(turn_id)

        return await self._run_in_use(
            lambda: run_in_thread(
                self._message_store.get_turn,
                normalized_turn_id,
            )
        )


    async def reconcile_turn(
        self,
        turn_id: UUID | str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Reconcile context and vector memory for a committed turn."""
        normalized_turn_id = self._validate_turn_id(turn_id)
        active_operation = operation or await self._operation_manager.create(
            OperationType.CONVERSATION_RECONCILE_TURN
        )
        return await active_operation.run(
            OperationType.CONVERSATION_RECONCILE_TURN,
            lambda active_operation: self._run_in_use(
                lambda: self._reconcile_turn(
                    normalized_turn_id,
                    operation=active_operation,
                )
            ),
        )


    async def _reconcile_turn(
        self,
        turn_id: str,
        *,
        operation: Operation,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            record = await run_in_thread(
                self._message_store.get_turn,
                turn_id,
            )
            if record is None:
                raise RavenError(
                    ErrorCode.CONVERSATION_TURN_NOT_FOUND,
                    f"Turn '{turn_id}' does not exist.",
                )
            await self._reload_context_memory()
            await self._ensure_turn_indexed(turn_id, operation=operation)
            updated = await run_in_thread(
                self._message_store.get_turn,
                turn_id,
            )
            return updated or record


    @staticmethod
    def _should_index_message(message: ChatMessage) -> bool:
        """Index user and final assistant text, not internal tool traces."""
        if message.role == MessageRole.USER:
            return bool(message.content)
        return message.role == MessageRole.ASSISTANT and bool(message.content)


    async def persist_messages(self) -> None:
        """Atomically persist the current model-facing context snapshot."""
        chat_store = self._chat_store
        if chat_store is None:
            raise RavenError(
                ErrorCode.CONVERSATION_MEMORY_NOT_INITIALIZED,
                "Conversation memory has not been initialized.",
            )
        async with self._memory_lock:
            messages = await run_in_thread(
                chat_store.get_messages,
                "messages",
            )
            await run_in_thread(
                self._message_store.replace_context,
                messages,
            )


    async def _reload_context_memory(self) -> None:
        memory = self._require_chat_memory()
        messages = await run_in_thread(
            self._message_store.get_context_messages
        )
        async with self._memory_lock:
            await run_in_thread(memory.set, messages)


    async def _ensure_turn_indexed(
        self,
        turn_id: str,
        *,
        operation: Operation | None = None,
    ) -> None:
        record = await run_in_thread(self._message_store.get_turn, turn_id)
        if record is None:
            raise RavenError(
                ErrorCode.CONVERSATION_TURN_NOT_FOUND,
                f"Turn '{turn_id}' does not exist.",
            )
        if record["memory_indexed"]:
            return

        await self._emit(
            operation,
            EventType.CONVERSATION_MEMORY_INDEX_STARTED,
            {
                "conversation_id": self.conversation_id,
                "turn_id": turn_id,
            },
        )
        try:
            messages = await run_in_thread(
                self._message_store.get_turn_messages,
                turn_id,
            )
            indexed_messages = [
                message
                for message in messages
                if self._should_index_message(message)
            ]
            if indexed_messages:
                vector_memory = self._require_vector_memory()
                async with self._memory_lock:
                    await run_in_thread(
                        self._index_turn,
                        vector_memory,
                        turn_id,
                        indexed_messages,
                    )
            await run_in_thread(
                self._message_store.mark_memory_indexed,
                turn_id,
            )
        except Exception as exc:
            await self._emit(
                operation,
                EventType.CONVERSATION_MEMORY_INDEX_FAILED,
                {
                    "conversation_id": self.conversation_id,
                    "turn_id": turn_id,
                    "error": error_payload(exc),
                },
            )
            raise

        await self._emit(
            operation,
            EventType.CONVERSATION_MEMORY_INDEX_COMPLETED,
            {
                "conversation_id": self.conversation_id,
                "turn_id": turn_id,
                "message_count": len(indexed_messages),
            },
        )


    @staticmethod
    def _index_turn(
        vector_memory: VectorMemory,
        turn_id: str,
        messages: Sequence[ChatMessage],
    ) -> None:
        payloads = [message.model_dump(mode="json") for message in messages]
        text = " ".join(
            str(message.content)
            for message in messages
            if message.content
        )
        node = TextNode(
            id_=turn_id,
            text=text,
            metadata={"sub_dicts": payloads},
            excluded_embed_metadata_keys=["sub_dicts"],
            excluded_llm_metadata_keys=["sub_dicts"],
        )
        vector_memory.vector_index.insert_nodes([node])


    @staticmethod
    def _validate_turn_id(turn_id: UUID | str) -> str:
        try:
            parsed = turn_id if isinstance(turn_id, UUID) else UUID(turn_id)
        except (TypeError, ValueError) as exc:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "turn_id must be a valid UUID.",
            ) from exc
        return str(parsed)


    @staticmethod
    def _validate_message_cursor(after_message_id: int) -> None:
        if (
            isinstance(after_message_id, bool)
            or not isinstance(after_message_id, int)
            or not 0 <= after_message_id <= SQLITE_MAX_INTEGER
        ):
            raise RavenError(
                ErrorCode.INVALID_MESSAGE_CURSOR,
                "after_message_id must be between 0 and SQLite's maximum integer.",
                details={"maximum": SQLITE_MAX_INTEGER},
            )


    @staticmethod
    def _turn_query(
        messages: Sequence[ChatMessage],
        user_query: str | None,
    ) -> str:
        if isinstance(user_query, str) and user_query.strip():
            return user_query.strip()
        for message in messages:
            if message.role == MessageRole.USER and message.content:
                return str(message.content).strip()
        raise RavenError(
            ErrorCode.INVALID_METADATA,
            "A committed conversation turn requires a user query.",
        )


    def get_preferences(self) -> list[dict[str, str]]:
        """Return copies of explicit conversation preferences."""
        self._ensure_started()
        return copy.deepcopy(self._preferences)


    async def list_preferences(self) -> list[dict[str, str]]:
        """Return preferences while safely pinning evictable storage."""
        async def load_preferences() -> list[dict[str, str]]:
            return self.get_preferences()

        return await self._run_in_use(load_preferences)


    async def save_preference(
        self,
        text: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.CONVERSATION_SAVE_PREFERENCE
        )
        return await active_operation.run(
            OperationType.CONVERSATION_SAVE_PREFERENCE,
            lambda active_operation: self._run_in_use(
                lambda: self._save_preference(
                    text,
                    operation=active_operation,
                )
            ),
        )


    async def _save_preference(
        self,
        text: str,
        *,
        operation: Operation,
    ) -> dict[str, str]:
        """Persist one explicit preference and return its stable record."""
        value = text.strip() if isinstance(text, str) else ""
        if not value:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "Preference must be a non-empty string.",
            )

        self._ensure_started()
        async with self._mutation_lock:
            existing = next(
                (
                    preference
                    for preference in self._preferences
                    if preference["text"] == value
                ),
                None,
            )
            if existing is not None:
                return copy.deepcopy(existing)

            preference = {"preference_id": str(uuid4()), "text": value}
            self._preferences.append(preference)
            try:
                await run_in_thread(self._persist_preferences)
            except Exception:
                self._preferences.pop()
                raise
            return copy.deepcopy(preference)


    async def remove_preference(
        self,
        preference_id: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.CONVERSATION_REMOVE_PREFERENCE
        )
        return await active_operation.run(
            OperationType.CONVERSATION_REMOVE_PREFERENCE,
            lambda active_operation: self._run_in_use(
                lambda: self._remove_preference(
                    preference_id,
                    operation=active_operation,
                )
            ),
        )


    async def _remove_preference(
        self,
        preference_id: str,
        *,
        operation: Operation,
    ) -> dict[str, str]:
        """Remove one explicit preference by its stable ID."""
        preference_id = self._validate_preference_id(preference_id)
        self._ensure_started()
        async with self._mutation_lock:
            index = next(
                (
                    index
                    for index, preference in enumerate(self._preferences)
                    if preference["preference_id"] == preference_id
                ),
                None,
            )
            if index is None:
                raise RavenError(
                    ErrorCode.PREFERENCE_NOT_FOUND,
                    f"Preference '{preference_id}' does not exist.",
                )

            removed = self._preferences.pop(index)
            try:
                await run_in_thread(self._persist_preferences)
            except Exception:
                self._preferences.insert(index, removed)
                raise
            return copy.deepcopy(removed)


    async def update(
        self,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.CONVERSATION_UPDATE
        )
        return await active_operation.run(
            OperationType.CONVERSATION_UPDATE,
            lambda active_operation: self._run_in_use(
                lambda: self._update(
                    title=title,
                    pinned=pinned,
                    operation=active_operation,
                )
            ),
        )


    async def _update(
        self,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        operation: Operation,
        only_if_untitled: bool = False,
    ) -> "Conversation":
        """Update and persist mutable conversation metadata."""
        if title is None and pinned is None:
            return self
        if title is not None:
            title = self._validate_title(title)

        await self._emit(
            operation,
            EventType.CONVERSATION_UPDATE_STARTED,
            {"conversation_id": self.conversation_id},
        )

        async with self._mutation_lock:
            if not only_if_untitled or not self.is_titled:
                previous_title = self.title
                previous_is_titled = self.is_titled
                previous_pinned = self.pinned
                if title is not None:
                    self.title = title
                    self.is_titled = True
                if pinned is not None:
                    self.pinned = pinned

                try:
                    await run_in_thread(self.write_metadata)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.title = previous_title
                    self.is_titled = previous_is_titled
                    self.pinned = previous_pinned
                    await self._emit(
                        operation,
                        EventType.CONVERSATION_UPDATE_FAILED,
                        {
                            "conversation_id": self.conversation_id,
                            "error": error_payload(exc),
                        },
                    )
                    raise

        await self._emit(
            operation,
            EventType.CONVERSATION_UPDATE_COMPLETED,
            self.to_dict(),
        )
        return self


    async def generate_title(
        self,
        llm: Any,
        first_user_message: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.CONVERSATION_GENERATE_TITLE
        )
        return await active_operation.run(
            OperationType.CONVERSATION_GENERATE_TITLE,
            lambda active_operation: self._run_in_use(
                lambda: self._generate_title(
                    llm,
                    first_user_message,
                    operation=active_operation,
                )
            ),
        )


    async def _generate_title(
        self,
        llm: Any,
        first_user_message: str,
        *,
        operation: Operation,
    ) -> str:
        """Generate and persist a title when this conversation is untitled."""
        async with self._title_lock:
            if self.is_titled:
                return self.title
            if llm is None:
                raise RavenError(
                    ErrorCode.LLM_MODEL_REQUIRED,
                    "An LLM is required to generate a conversation title.",
                )

            message = (
                first_user_message.strip()
                if isinstance(first_user_message, str)
                else ""
            )
            if not message:
                raise RavenError(
                    ErrorCode.INVALID_METADATA,
                    "first_user_message must be a non-empty string.",
                )

            response = await llm.achat(
                [
                    ChatMessage(
                        role=MessageRole.SYSTEM,
                        content=TITLE_SYSTEM_PROMPT,
                    ),
                    ChatMessage(
                        role=MessageRole.USER,
                        content=message,
                    ),
                ]
            )
            title = self._title_from_response(response)
            update_task = await operation.run(
                OperationType.CONVERSATION_UPDATE,
                lambda active_operation: self._run_in_use(
                    lambda: self._update(
                        title=title,
                        operation=active_operation,
                        only_if_untitled=True,
                    )
                ),
            )
            await update_task.result()
            return self.title


    def to_dict(self) -> dict[str, Any]:
        """Return the stable public metadata representation."""
        if self.created_at is None:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "Conversation metadata has not been initialized.",
            )
        return {
            "schema_version": PERSISTENCE_VERSION,
            "conversation_id": self.conversation_id,
            "type": self.type,
            "knowledge_name": self.knowledge_name,
            "title": self.title,
            "is_titled": self.is_titled,
            "pinned": self.pinned,
            "created_at": self.created_at.isoformat(),
        }


    def write_metadata(self) -> None:
        """Atomically persist conversation metadata."""
        self.dir_path.mkdir(parents=True, exist_ok=True)
        temporary_path = self.metadata_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary_path, self.metadata_path)


    def _open_storage(self) -> None:
        self.dir_path.mkdir(parents=True, exist_ok=True)
        self.qdrant_dir.mkdir(parents=True, exist_ok=True)
        self._preferences = self._load_preferences()
        self._message_store.open()
        try:
            self._open_qdrant()
        except Exception:
            self._message_store.close()
            raise


    async def _open_storage_safely(self) -> None:
        """Finish and roll back a threaded open before propagating cancellation."""
        if self._storage_cleanup_pending:
            await run_in_thread(self._close_open_storage)
        opening = asyncio.create_task(asyncio.to_thread(self._open_storage))
        try:
            _, cancellation_requested = await await_completion(opening)
        except BaseException:
            await self._rollback_open_storage()
            raise
        if cancellation_requested:
            await self._rollback_open_storage()
            raise asyncio.CancelledError


    async def _rollback_open_storage(self) -> None:
        cleanup = asyncio.create_task(asyncio.to_thread(self._close_open_storage))
        _, cancellation_requested = await await_completion(cleanup)
        if cancellation_requested:
            raise asyncio.CancelledError


    def _close_open_storage(self) -> None:
        self._storage_cleanup_pending = True
        qdrant = self._qdrant or self._pending_qdrant_close
        self._qdrant = None
        self._pending_qdrant_close = qdrant
        self._chat_store = None
        self._chat_memory = None
        self._vector_memory = None
        self._memory_configuration = None
        cleanup_error: BaseException | None = None
        try:
            self._message_store.close()
        except BaseException as exc:
            cleanup_error = exc
        if qdrant is not None:
            try:
                qdrant.close()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
            else:
                self._pending_qdrant_close = None
        if cleanup_error is not None:
            raise cleanup_error
        self._storage_cleanup_pending = False


    def _open_qdrant(self) -> None:
        if self._qdrant is None:
            self._qdrant = QdrantClient(path=str(self.qdrant_dir))


    def _load_chat_store(self) -> SimpleChatStore:
        messages = self._message_store.get_context_messages()
        return SimpleChatStore(
            store={"messages": messages} if messages else {}
        )


    def _load_preferences(self) -> list[dict[str, str]]:
        if not self.preferences_path.exists():
            return []

        try:
            data = json.loads(self.preferences_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                f"Preferences for conversation '{self.conversation_id}' could not be read.",
            ) from exc

        if not isinstance(data, dict):
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                f"Invalid preferences for conversation '{self.conversation_id}'.",
            )

        if data.get("schema_version", PREFERENCE_SCHEMA_VERSION) != PREFERENCE_SCHEMA_VERSION:
            raise RavenError(
                ErrorCode.UNSUPPORTED_METADATA_VERSION,
                f"Unsupported preferences version for conversation '{self.conversation_id}'.",
            )

        preferences = data.get("preferences")
        if not isinstance(preferences, list):
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                f"Invalid preferences for conversation '{self.conversation_id}'.",
            )

        if all(isinstance(value, str) for value in preferences):
            migrated = self._migrate_preferences(preferences)
            self._write_preferences(migrated)
            return migrated

        loaded: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        seen_texts: set[str] = set()
        for preference in preferences:
            if not isinstance(preference, dict):
                raise RavenError(
                    ErrorCode.INVALID_METADATA,
                    f"Invalid preference in conversation '{self.conversation_id}'.",
                )
            preference_id = self._validate_preference_id(
                preference.get("preference_id")
            )
            text = preference.get("text")
            if not isinstance(text, str) or not text.strip():
                raise RavenError(
                    ErrorCode.INVALID_METADATA,
                    f"Invalid preference text in conversation '{self.conversation_id}'.",
                )
            text = text.strip()
            if preference_id in seen_ids or text in seen_texts:
                continue
            seen_ids.add(preference_id)
            seen_texts.add(text)
            loaded.append({"preference_id": preference_id, "text": text})
        return loaded


    def _persist_preferences(self) -> None:
        self._write_preferences(self._preferences)


    def _write_preferences(self, preferences: list[dict[str, str]]) -> None:
        temporary_path = self.preferences_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(
                {
                    "schema_version": PREFERENCE_SCHEMA_VERSION,
                    "preferences": preferences,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(temporary_path, self.preferences_path)


    @staticmethod
    def _migrate_preferences(values: list[str]) -> list[dict[str, str]]:
        migrated: list[dict[str, str]] = []
        seen: set[str] = set()
        for value in values:
            text = value.strip()
            if text and text not in seen:
                migrated.append({"preference_id": str(uuid4()), "text": text})
                seen.add(text)
        return migrated


    @staticmethod
    def _validate_preference_id(preference_id: Any) -> str:
        if not isinstance(preference_id, str):
            raise RavenError(
                ErrorCode.INVALID_PREFERENCE_ID,
                "preference_id must be a UUID string.",
            )
        try:
            parsed = UUID(preference_id)
        except ValueError as exc:
            raise RavenError(
                ErrorCode.INVALID_PREFERENCE_ID,
                "preference_id must be a UUID string.",
            ) from exc
        if str(parsed) != preference_id:
            raise RavenError(
                ErrorCode.INVALID_PREFERENCE_ID,
                "preference_id must use the canonical UUID format.",
            )
        return preference_id


    def _ensure_memory_collection(self, embed_model: Any) -> None:
        qdrant = self._require_qdrant()
        probe = embed_model.get_text_embedding("raven conversation memory probe")
        if qdrant.collection_exists(MEMORY_COLLECTION):
            collection = qdrant.get_collection(MEMORY_COLLECTION)
            vectors = collection.config.params.vectors
            vector_config = (
                vectors.get(MEMORY_VECTOR_NAME)
                if isinstance(vectors, dict)
                else vectors
            )
            current_dimension = getattr(vector_config, "size", None)
            if current_dimension != len(probe):
                raise RavenError(
                    ErrorCode.EMBEDDING_DIMENSION_MISMATCH,
                    f"Embedding dimension mismatch for conversation '{self.conversation_id}'.",
                    details={
                        "collection_dimension": current_dimension,
                        "model_dimension": len(probe),
                    },
                )
            return

        qdrant.create_collection(
            collection_name=MEMORY_COLLECTION,
            vectors_config={
                MEMORY_VECTOR_NAME: qdrant_models.VectorParams(
                    size=len(probe),
                    distance=qdrant_models.Distance.COSINE,
                )
            },
        )


    def _ensure_embedding_identity(self, embed_model: Any) -> None:
        current = embedding_identity(embed_model)
        stored = self._message_store.get_setting("embedding_identity")
        if stored is None:
            self._message_store.set_setting("embedding_identity", current)
            return
        if stored != current:
            raise RavenError(
                ErrorCode.EMBEDDING_IDENTITY_MISMATCH,
                f"Embedding model does not match conversation '{self.conversation_id}'.",
                details={"conversation_id": self.conversation_id},
            )


    def _require_qdrant(self) -> QdrantClient:
        self._ensure_started()
        if self._qdrant is None:
            raise RavenError(
                ErrorCode.CONVERSATION_NOT_STARTED,
                f"Conversation '{self.conversation_id}' storage is not open.",
            )
        return self._qdrant


    def _require_chat_memory(self) -> ChatSummaryMemoryBuffer:
        self._ensure_started()
        if self._chat_memory is None:
            raise RavenError(
                ErrorCode.CONVERSATION_MEMORY_NOT_INITIALIZED,
                f"Memory for conversation '{self.conversation_id}' is not initialized.",
            )
        return self._chat_memory


    def _require_vector_memory(self) -> VectorMemory:
        self._ensure_started()
        if self._vector_memory is None:
            raise RavenError(
                ErrorCode.CONVERSATION_MEMORY_NOT_INITIALIZED,
                f"Memory for conversation '{self.conversation_id}' is not initialized.",
            )
        return self._vector_memory


    def _ensure_started(self) -> None:
        if self._closed:
            raise RavenError(
                ErrorCode.CONVERSATION_CLOSED,
                f"Conversation '{self.conversation_id}' is closed.",
            )
        if not self._started:
            raise RavenError(
                ErrorCode.CONVERSATION_NOT_STARTED,
                f"Conversation '{self.conversation_id}' has not been started.",
            )


    @staticmethod
    def _validate_title(title: str) -> str:
        if not isinstance(title, str) or not title.strip():
            raise RavenError(
                ErrorCode.INVALID_CONVERSATION_TITLE,
                "Conversation title must be a non-empty string.",
            )
        return title.strip()


    @classmethod
    def _title_from_response(cls, response: Any) -> str:
        message = getattr(response, "message", response)
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            raise RavenError(
                ErrorCode.INVALID_CONVERSATION_TITLE,
                "The title model returned no usable title.",
            )

        title = content.strip().splitlines()[0].strip()
        if title.lower().startswith("title:"):
            title = title[6:].strip()
        title = title.strip("\"'` ")
        return cls._validate_title(title)


    @staticmethod
    async def _emit(
        operation: Operation | None,
        event_type: EventType,
        data: dict[str, Any],
    ) -> None:
        if operation is not None:
            await operation.publish(Event(type=event_type, data=data))


    @classmethod
    def from_dir(
        cls,
        dir_path: Path,
        *,
        operation_manager: OperationManager,
        before_open: Callable[["Conversation"], Awaitable[None]] | None = None,
        after_open: Callable[["Conversation"], Awaitable[None]] | None = None,
    ) -> "Conversation":
        """Load and validate a conversation from its metadata file."""
        directory = Path(dir_path)
        metadata_path = directory / "metadata.json"
        if not metadata_path.is_file():
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                f"Conversation directory '{directory.name}' has no metadata.json file.",
            )
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                f"Metadata for conversation '{directory.name}' could not be read.",
            ) from exc
        if not isinstance(data, dict):
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                f"Invalid metadata for conversation '{directory.name}'.",
            )
        if "schema_version" not in data:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                f"Metadata for conversation '{directory.name}' has no schema version.",
            )
        version = data["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                f"Metadata for conversation '{directory.name}' has an invalid schema version.",
            )
        if version != PERSISTENCE_VERSION:
            raise RavenError(
                ErrorCode.UNSUPPORTED_METADATA_VERSION,
                f"Unsupported metadata version for conversation '{directory.name}'.",
                details={"schema_version": version},
            )
        expected_fields = {
            "schema_version",
            "conversation_id",
            "type",
            "knowledge_name",
            "title",
            "is_titled",
            "pinned",
            "created_at",
        }
        if set(data) != expected_fields:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                f"Metadata for conversation '{directory.name}' has invalid fields.",
            )

        conversation_id = data.get("conversation_id")
        if not isinstance(conversation_id, str) or not _CONVERSATION_ID.fullmatch(
            conversation_id
        ):
            raise RavenError(
                ErrorCode.INVALID_CONVERSATION_ID,
                "Conversation metadata contains an invalid conversation ID.",
            )
        if conversation_id != directory.name:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                f"Conversation metadata ID does not match directory '{directory.name}'.",
            )

        created_at = data.get("created_at")
        try:
            parsed_created_at = datetime.fromisoformat(created_at)
        except (TypeError, ValueError) as exc:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "Conversation metadata contains an invalid created_at value.",
            ) from exc

        if parsed_created_at.tzinfo is None:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "Conversation metadata contains a timezone-naive created_at value.",
            )

        title = data.get("title")
        if not isinstance(title, str) or not title.strip():
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "Conversation metadata contains an invalid title.",
            )

        is_titled = data.get("is_titled")
        if not isinstance(is_titled, bool):
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "Conversation metadata contains an invalid is_titled value.",
            )

        knowledge_name = data.get("knowledge_name")
        if knowledge_name is not None and (
            not isinstance(knowledge_name, str) or not knowledge_name.strip()
        ):
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "Conversation metadata contains an invalid knowledge_name.",
            )
        pinned = data.get("pinned")
        if not isinstance(pinned, bool):
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "Conversation metadata contains an invalid pinned value.",
            )
        expected_type = "local" if knowledge_name is not None else "global"
        if data.get("type") != expected_type:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "Conversation metadata type does not match its knowledge scope.",
            )

        conversation = cls(
            directory,
            operation_manager=operation_manager,
            before_open=before_open,
            after_open=after_open,
        )
        conversation._set_metadata(
            conversation_id=conversation_id,
            knowledge_name=knowledge_name,
            title=title,
            is_titled=is_titled,
            pinned=pinned,
            created_at=parsed_created_at,
        )
        return conversation




class ConversationManager:
    """Registry and CRUD manager for persisted conversations."""

    def __init__(
        self,
        paths: PathConfig,
        knowledge_base: KnowledgeBase,
        operation_manager: OperationManager,
        *,
        max_open_conversations: int = DEFAULT_OPEN_CONVERSATION_LIMIT,
    ) -> None:
        if (
            isinstance(max_open_conversations, bool)
            or not isinstance(max_open_conversations, int)
            or max_open_conversations <= 0
        ):
            raise RavenError(
                ErrorCode.INVALID_RESOURCE_CACHE_SIZE,
                "Open conversation limit must be a positive integer.",
            )
        self.conversation_dir = paths.conversations_dir
        self._knowledge_base = knowledge_base
        self._operation_manager = operation_manager
        self.max_open_conversations = max_open_conversations
        self._conversations: OrderedDict[str, Conversation] = OrderedDict()
        self._discovery_issues: OrderedDict[str, DiscoveryIssue] = OrderedDict()
        self._lifecycle_lock = asyncio.Lock()
        self._resource_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self._opening_conversations: set[int] = set()
        self._started = False
        self._closed = False


    @property
    def is_started(self) -> bool:
        return self._started and not self._closed


    async def start(self) -> None:
        """Discover existing conversation directories."""
        async with self._lifecycle_lock:
            if self.is_started:
                return
            if self._closed:
                raise RavenError(
                    ErrorCode.CONVERSATION_CLOSED,
                    "Conversation manager is closed.",
                )

            await asyncio.to_thread(
                self.conversation_dir.mkdir,
                parents=True,
                exist_ok=True,
            )
            await asyncio.to_thread(self._cleanup_deletion_tombstones)
            conversation_paths = await asyncio.to_thread(self._scan_existing)
            loaded: OrderedDict[str, Conversation] = OrderedDict()
            issues: OrderedDict[str, DiscoveryIssue] = OrderedDict()
            for path in conversation_paths:
                try:
                    conversation = await asyncio.to_thread(
                        Conversation.from_dir,
                        path,
                        operation_manager=self._operation_manager,
                        before_open=self._before_conversation_open,
                        after_open=self._after_conversation_open,
                    )
                    if conversation.conversation_id in loaded:
                        raise RavenError(
                            ErrorCode.INVALID_METADATA,
                            "Duplicate conversation identity "
                            f"'{conversation.conversation_id}' was discovered.",
                        )
                    loaded[conversation.conversation_id] = conversation
                except Exception as exc:
                    issues[path.name] = DiscoveryIssue.from_error(
                        "conversation",
                        path.name,
                        exc,
                    )

            self._conversations = loaded
            self._discovery_issues = issues
            self._started = True


    async def close(self) -> None:
        """Close every loaded conversation and release the registry."""
        async with self._lifecycle_lock:
            if self._closed and not self._conversations:
                return
            self._closed = True
            self._started = False
            resources = list(self._conversations.items())
            results = await asyncio.gather(
                *(conversation.close() for _, conversation in resources),
                return_exceptions=True,
            )
            failures = [
                (conversation_id, conversation, result)
                for (conversation_id, conversation), result in zip(resources, results)
                if isinstance(result, BaseException)
            ]
            self._conversations = OrderedDict(
                (conversation_id, conversation)
                for conversation_id, conversation, _ in failures
            )
            if failures:
                raise RavenError(
                    ErrorCode.INTERNAL_ERROR,
                    "One or more conversations could not be closed.",
                    details={"failure_count": len(failures)},
                ) from failures[0][2]


    async def create(
        self,
        knowledge_name: str | None = None,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.CONVERSATION_CREATE
        )
        return await active_operation.run(
            OperationType.CONVERSATION_CREATE,
            lambda active_operation: self._create(
                knowledge_name,
                operation=active_operation,
            ),
        )


    async def _create(
        self,
        knowledge_name: str | None = None,
        *,
        operation: Operation,
    ) -> Conversation:
        """Create, persist, and register a new conversation."""
        self._ensure_started()
        if knowledge_name is not None:
            self._validate_knowledge(knowledge_name)

        await self._emit(
            operation,
            EventType.CONVERSATION_CREATE_STARTED,
            {"knowledge_name": knowledge_name},
        )

        try:
            async with self._mutation_lock:
                while True:
                    conversation_id = uuid4().hex[:12]
                    if not (self.conversation_dir / conversation_id).exists():
                        break
                conversation = Conversation(
                    self.conversation_dir / conversation_id,
                    operation_manager=self._operation_manager,
                    before_open=self._before_conversation_open,
                    after_open=self._after_conversation_open,
                )
                conversation._set_metadata(
                    conversation_id=conversation_id,
                    knowledge_name=knowledge_name,
                    title=DEFAULT_TITLE,
                    is_titled=False,
                    pinned=False,
                    created_at=datetime.now(timezone.utc),
                )
                await asyncio.to_thread(conversation.write_metadata)
                self._conversations[conversation_id] = conversation
                self._conversations.move_to_end(conversation_id)
                self._discovery_issues.pop(conversation_id, None)

            await self._emit(
                operation,
                EventType.CONVERSATION_CREATE_COMPLETED,
                conversation.to_dict(),
            )
            return conversation
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                operation,
                EventType.CONVERSATION_CREATE_FAILED,
                {"error": error_payload(exc)},
            )
            raise


    def get(self, conversation_id: str) -> Conversation:
        """Return one registered conversation, opening storage lazily when needed."""
        self._ensure_started()
        conversation_id = self._validate_id(conversation_id)
        try:
            conversation = self._conversations[conversation_id]
        except KeyError as exc:
            issue = self._discovery_issues.get(conversation_id)
            if issue is not None:
                raise issue.as_error() from exc
            raise RavenError(
                ErrorCode.CONVERSATION_NOT_FOUND,
                f"Conversation '{conversation_id}' does not exist.",
            ) from exc
        self._conversations.move_to_end(conversation_id)
        return conversation


    def list_discovery_issues(self) -> list[DiscoveryIssue]:
        """Return persisted conversation directories that could not be loaded."""
        self._ensure_started()
        return list(self._discovery_issues.values())


    def owns(self, conversation: Conversation) -> bool:
        """Return whether this exact object belongs to this registry."""
        self._ensure_started()
        return self._conversations.get(conversation.conversation_id) is conversation


    def list(self) -> list[dict[str, Any]]:
        """Return metadata for all opened conversations."""
        self._ensure_started()
        return [
            conversation.to_dict()
            for conversation in self._conversations.values()
        ]


    def list_page(
        self,
        limit: int,
        after_conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return newest-first metadata with a stable conversation cursor."""
        self._ensure_started()
        records = sorted(
            (conversation.to_dict() for conversation in self._conversations.values()),
            key=lambda item: (item["created_at"], item["conversation_id"]),
            reverse=True,
        )
        start = 0
        if after_conversation_id is not None:
            start = next(
                (
                    index + 1
                    for index, item in enumerate(records)
                    if item["conversation_id"] == after_conversation_id
                ),
                -1,
            )
            if start < 0:
                raise RavenError(
                    ErrorCode.CONVERSATION_NOT_FOUND,
                    f"Conversation cursor '{after_conversation_id}' does not exist.",
                )
        return records[start:start + limit]


    async def update(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.CONVERSATION_UPDATE_METADATA
        )
        return await active_operation.run(
            OperationType.CONVERSATION_UPDATE_METADATA,
            lambda active_operation: self._update_metadata(
                conversation_id,
                title=title,
                pinned=pinned,
                operation=active_operation,
            ),
        )


    async def _update_metadata(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        operation: Operation,
    ) -> Conversation:
        """Update mutable conversation metadata."""
        conversation = self.get(conversation_id)
        update_task = await conversation.update(
            title=title,
            pinned=pinned,
            operation=operation,
        )
        return await update_task.result()


    async def delete(
        self,
        conversation_id: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.CONVERSATION_DELETE
        )
        return await active_operation.run(
            OperationType.CONVERSATION_DELETE,
            lambda active_operation: self._delete(
                conversation_id,
                operation=active_operation,
            ),
        )


    async def _delete(
        self,
        conversation_id: str,
        *,
        operation: Operation,
    ) -> None:
        """Remove a conversation from the registry and disk."""
        conversation = self.get(conversation_id)
        if conversation.has_active_turn:
            raise RavenError(
                ErrorCode.CONVERSATION_TURN_ACTIVE,
                f"Conversation '{conversation_id}' has an active turn.",
            )
        await self._emit(
            operation,
            EventType.CONVERSATION_DELETE_STARTED,
            {"conversation_id": conversation.conversation_id},
        )

        try:
            async with self._mutation_lock:
                await conversation.close()
                tombstone = self.conversation_dir / (
                    f".deleting-{conversation.conversation_id}"
                )
                await asyncio.to_thread(
                    os.replace,
                    conversation.dir_path,
                    tombstone,
                )
                self._conversations.pop(conversation.conversation_id, None)
                await asyncio.to_thread(shutil.rmtree, tombstone)

            await self._emit(
                operation,
                EventType.CONVERSATION_DELETE_COMPLETED,
                {"conversation_id": conversation.conversation_id},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                operation,
                EventType.CONVERSATION_DELETE_FAILED,
                {
                    "conversation_id": conversation.conversation_id,
                    "error": error_payload(exc),
                },
            )
            raise


    def _scan_existing(self) -> list[Path]:
        if not self.conversation_dir.is_dir():
            return []
        return sorted(
            (
                entry
                for entry in self.conversation_dir.iterdir()
                if entry.is_dir() and not entry.name.startswith(".deleting-")
            ),
            key=lambda entry: entry.name,
        )


    def _cleanup_deletion_tombstones(self) -> None:
        if not self.conversation_dir.is_dir():
            return
        for entry in self.conversation_dir.iterdir():
            if entry.is_dir() and entry.name.startswith(".deleting-"):
                shutil.rmtree(entry)


    async def _before_conversation_open(self, target: Conversation) -> None:
        async with self._resource_lock:
            if target.conversation_id in self._conversations:
                self._conversations.move_to_end(target.conversation_id)
            unavailable: set[int] = set()
            while True:
                opened = [
                    conversation
                    for conversation in self._conversations.values()
                    if conversation.is_started and conversation is not target
                    and id(conversation) not in self._opening_conversations
                ]
                if (
                    len(opened) + len(self._opening_conversations)
                    < self.max_open_conversations
                ):
                    break
                candidate = next(
                    (
                        conversation
                        for conversation in opened
                        if not conversation.has_active_turn
                        and conversation.active_uses == 0
                        and id(conversation) not in unavailable
                    ),
                    None,
                )
                if candidate is None:
                    break
                if not await candidate.release_resources():
                    unavailable.add(id(candidate))
            self._opening_conversations.add(id(target))


    async def _after_conversation_open(self, target: Conversation) -> None:
        async with self._resource_lock:
            self._opening_conversations.discard(id(target))


    def _validate_knowledge(self, knowledge_name: str) -> None:
        if not isinstance(knowledge_name, str) or not knowledge_name.strip():
            raise RavenError(
                ErrorCode.INVALID_KNOWLEDGE_NAME,
                "knowledge_name must be a non-empty string.",
            )
        self._knowledge_base.get(knowledge_name)


    @staticmethod
    def _validate_id(conversation_id: str) -> str:
        if not isinstance(conversation_id, str) or not _CONVERSATION_ID.fullmatch(
            conversation_id
        ):
            raise RavenError(
                ErrorCode.INVALID_CONVERSATION_ID,
                "Conversation ID has an invalid format.",
            )
        return conversation_id


    def _ensure_started(self) -> None:
        if self._closed:
            raise RavenError(
                ErrorCode.CONVERSATION_CLOSED,
                "Conversation manager is closed.",
            )
        if not self._started:
            raise RavenError(
                ErrorCode.CONVERSATION_NOT_STARTED,
                "Conversation manager has not been started.",
            )


    @staticmethod
    async def _emit(
        operation: Operation | None,
        event_type: EventType,
        data: dict[str, Any],
    ) -> None:
        if operation is not None:
            await operation.publish(Event(type=event_type, data=data))
