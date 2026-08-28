"""Conversation persistence and lifecycle management for Raven."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shutil
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from llama_index.core.base.llms.types import MessageRole
from llama_index.core.llms import ChatMessage
from llama_index.core.memory import ChatSummaryMemoryBuffer, VectorMemory
from llama_index.core.storage.chat_store import SimpleChatStore
from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models

from ..core.config import PathConfig
from ..core.errors import ErrorCode, RavenError, error_payload
from ..core.events import Event, EventType
from ..core.operations import Operation, OperationManager, OperationTask, OperationType
from .knowledge_base import KnowledgeBase


DEFAULT_TITLE = "New Conversation"
TITLE_SYSTEM_PROMPT = (
    "Create a concise title for this conversation from the user's first message. "
    "Return only the title, without quotation marks, labels, or explanation."
)
PERSISTENCE_VERSION = 1
PREFERENCE_SCHEMA_VERSION = 1
MEMORY_COLLECTION = "messages"
MEMORY_VECTOR_NAME = "text-dense"
_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class Conversation:
    """Durable identity and metadata for one conversation."""

    def __init__(
        self,
        dir_path: Path,
        *,
        operation_manager: OperationManager,
    ) -> None:
        self.dir_path = Path(dir_path)
        self.conversation_id = ""
        self.knowledge_name: str | None = None
        self.title = DEFAULT_TITLE
        self.is_titled = False
        self.pinned = False
        self.created_at: datetime | None = None

        self.metadata_path = self.dir_path / "metadata.json"
        self.messages_path = self.dir_path / "messages.json"
        self.preferences_path = self.dir_path / "preferences.json"
        self.qdrant_dir = self.dir_path / "qdrant"

        self._qdrant: QdrantClient | None = None
        self._chat_store: SimpleChatStore | None = None
        self._chat_memory: ChatSummaryMemoryBuffer | None = None
        self._vector_memory: VectorMemory | None = None
        self._preferences: list[dict[str, str]] = []
        self._lifecycle_lock = asyncio.Lock()
        self._memory_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._operation_manager = operation_manager


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


    async def start(self) -> None:
        """Open the conversation storage without initializing model memory."""
        async with self._lifecycle_lock:
            if self.is_started:
                return
            if self._closed:
                raise RavenError(
                    ErrorCode.CONVERSATION_CLOSED,
                    f"Conversation '{self.conversation_id}' is closed.",
                )

            await asyncio.to_thread(self._open_storage)
            self._started = True


    async def close(self) -> None:
        """Close the conversation Qdrant database and memory resources."""
        async with self._lifecycle_lock:
            if self._closed:
                return

            qdrant = self._qdrant
            self._qdrant = None
            self._chat_store = None
            self._chat_memory = None
            self._vector_memory = None
            self._closed = True
            self._started = False

            if qdrant is not None:
                await asyncio.to_thread(qdrant.close)


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
            if self._chat_memory is not None:
                return

            chat_store = await asyncio.to_thread(self._load_chat_store)
            await asyncio.to_thread(self._ensure_memory_collection, embed_model)
            qdrant = self._require_qdrant()
            vector_store = QdrantVectorStore(
                collection_name=MEMORY_COLLECTION,
                client=qdrant,
                dense_vector_name=MEMORY_VECTOR_NAME,
            )

            self._chat_store = chat_store
            self._chat_memory = ChatSummaryMemoryBuffer.from_defaults(
                llm=llm,
                chat_store=chat_store,
                chat_store_key="messages",
                token_limit=token_limit,
                count_initial_tokens=True,
            )
            self._vector_memory = VectorMemory.from_defaults(
                vector_store=vector_store,
                embed_model=embed_model,
                retriever_kwargs={"similarity_top_k": memory_top_k},
            )


    async def get_context_messages(
        self,
        *,
        initial_token_count: int = 0,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Start a context-loading task for this conversation."""
        return await self._operation_manager.run(
            OperationType.CONVERSATION_GET_CONTEXT,
            lambda active_operation: self._get_context_messages(
                initial_token_count=initial_token_count,
                operation=active_operation,
            ),
            operation=operation,
        )


    async def _get_context_messages(
        self,
        *,
        initial_token_count: int,
        operation: Operation,
    ) -> list[ChatMessage]:
        """Load context-fit messages and compact older history when needed."""
        memory = self._require_chat_memory()
        async with self._memory_lock:
            messages_before = await asyncio.to_thread(memory.get_all)
            message_count_before = len(messages_before)
            compaction_needed = await asyncio.to_thread(
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
                await self.persist_messages()
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
        memory = self._require_chat_memory()
        async with self._memory_lock:
            return await asyncio.to_thread(memory.get_all)


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
        """Add one message to chat history and optionally semantic memory."""
        memory = self._require_chat_memory()
        vector_memory = self._require_vector_memory() if index_in_vector_memory else None
        async with self._mutation_lock:
            await asyncio.to_thread(memory.put, message)
            if vector_memory is not None:
                await vector_memory.aput(message)


    async def append_turn(self, messages: Sequence[ChatMessage]) -> None:
        """Append one complete model turn and persist it with one disk write."""
        if not messages:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "A conversation turn must contain at least one message.",
            )

        memory = self._require_chat_memory()
        chat_store = self._chat_store
        if chat_store is None:
            raise RavenError(
                ErrorCode.CONVERSATION_MEMORY_NOT_INITIALIZED,
                "Conversation chat storage has not been initialized.",
            )
        vector_memory = self._require_vector_memory()
        async with self._mutation_lock:
            for message in messages:
                await asyncio.to_thread(memory.put, message)
                if self._should_index_message(message):
                    await vector_memory.aput(message)
            await asyncio.to_thread(self._persist_chat_store, chat_store)


    @staticmethod
    def _should_index_message(message: ChatMessage) -> bool:
        """Index user and final assistant text, not internal tool traces."""
        if message.role == MessageRole.USER:
            return bool(message.content)
        return message.role == MessageRole.ASSISTANT and bool(message.content)


    async def persist_messages(self) -> None:
        """Atomically persist the current chat store to messages.json."""
        chat_store = self._chat_store
        if chat_store is None:
            raise RavenError(
                ErrorCode.CONVERSATION_MEMORY_NOT_INITIALIZED,
                "Conversation memory has not been initialized.",
            )
        await asyncio.to_thread(self._persist_chat_store, chat_store)


    def get_preferences(self) -> list[dict[str, str]]:
        """Return copies of explicit conversation preferences."""
        self._ensure_started()
        return copy.deepcopy(self._preferences)


    async def save_preference(
        self,
        text: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self._operation_manager.run(
            OperationType.CONVERSATION_SAVE_PREFERENCE,
            lambda active_operation: self._save_preference(
                text,
                operation=active_operation,
            ),
            operation=operation,
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
                await asyncio.to_thread(self._persist_preferences)
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
        return await self._operation_manager.run(
            OperationType.CONVERSATION_REMOVE_PREFERENCE,
            lambda active_operation: self._remove_preference(
                preference_id,
                operation=active_operation,
            ),
            operation=operation,
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
                await asyncio.to_thread(self._persist_preferences)
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
        return await self._operation_manager.run(
            OperationType.CONVERSATION_UPDATE,
            lambda active_operation: self._update(
                title=title,
                pinned=pinned,
                operation=active_operation,
            ),
            operation=operation,
        )


    async def _update(
        self,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        operation: Operation,
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
            previous_title = self.title
            previous_is_titled = self.is_titled
            previous_pinned = self.pinned
            if title is not None:
                self.title = title
                self.is_titled = True
            if pinned is not None:
                self.pinned = pinned

            try:
                await asyncio.to_thread(self.write_metadata)
            except asyncio.CancelledError:
                self.title = previous_title
                self.is_titled = previous_is_titled
                self.pinned = previous_pinned
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
        return await self._operation_manager.run(
            OperationType.CONVERSATION_GENERATE_TITLE,
            lambda active_operation: self._generate_title(
                llm,
                first_user_message,
                operation=active_operation,
            ),
            operation=operation,
        )


    async def _generate_title(
        self,
        llm: Any,
        first_user_message: str,
        *,
        operation: Operation,
    ) -> str:
        """Generate and persist a title when this conversation is untitled."""
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

        if self.is_titled:
            return self.title

        update_task = await self.update(title=title, operation=operation)
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
        self._qdrant = QdrantClient(path=str(self.qdrant_dir))


    def _load_chat_store(self) -> SimpleChatStore:
        if self.messages_path.exists():
            return SimpleChatStore.from_persist_path(str(self.messages_path))
        return SimpleChatStore()


    def _persist_chat_store(self, chat_store: SimpleChatStore) -> None:
        temporary_path = self.messages_path.with_suffix(".json.tmp")
        serialized = json.dumps(
            chat_store.model_dump(mode="json"),
            indent=2,
            ensure_ascii=False,
        )
        temporary_path.write_text(serialized + "\n", encoding="utf-8")
        os.replace(temporary_path, self.messages_path)


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
    ) -> "Conversation":
        """Load and validate a conversation from its metadata file."""
        metadata_path = Path(dir_path) / "metadata.json"
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                f"Invalid conversation metadata in '{metadata_path}'.",
            )
        if data.get("schema_version", PERSISTENCE_VERSION) != PERSISTENCE_VERSION:
            raise RavenError(
                ErrorCode.UNSUPPORTED_METADATA_VERSION,
                f"Unsupported conversation metadata version in '{metadata_path}'.",
            )

        conversation_id = data.get("conversation_id")
        if not isinstance(conversation_id, str) or not _CONVERSATION_ID.fullmatch(
            conversation_id
        ):
            raise RavenError(
                ErrorCode.INVALID_CONVERSATION_ID,
                "Conversation metadata contains an invalid conversation ID.",
            )

        created_at = data.get("created_at")
        try:
            parsed_created_at = (
                datetime.fromisoformat(created_at)
                if isinstance(created_at, str)
                else datetime.now(timezone.utc)
            )
        except ValueError as exc:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "Conversation metadata contains an invalid created_at value.",
            ) from exc

        if parsed_created_at.tzinfo is None:
            parsed_created_at = parsed_created_at.replace(tzinfo=timezone.utc)

        title = data.get("title", DEFAULT_TITLE)
        if not isinstance(title, str) or not title.strip():
            title = DEFAULT_TITLE

        is_titled = data.get("is_titled")
        if not isinstance(is_titled, bool):
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "Conversation metadata contains an invalid is_titled value.",
            )

        knowledge_name = data.get("knowledge_name")
        if knowledge_name is not None and not isinstance(knowledge_name, str):
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "Conversation metadata contains an invalid knowledge_name.",
            )

        conversation = cls(
            Path(dir_path),
            operation_manager=operation_manager,
        )
        conversation._set_metadata(
            conversation_id=conversation_id,
            knowledge_name=knowledge_name,
            title=title,
            is_titled=is_titled,
            pinned=bool(data.get("pinned", False)),
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
    ) -> None:
        self.conversation_dir = paths.conversations_dir
        self._knowledge_base = knowledge_base
        self._operation_manager = operation_manager
        self._conversations: dict[str, Conversation] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
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
            conversation_paths = await asyncio.to_thread(self._scan_existing)
            loaded: dict[str, Conversation] = {}
            for path in conversation_paths:
                conversation = await asyncio.to_thread(
                    Conversation.from_dir,
                    path,
                    operation_manager=self._operation_manager,
                )
                loaded[conversation.conversation_id] = conversation

            self._conversations = loaded
            self._started = True


    async def close(self) -> None:
        """Close every loaded conversation and release the registry."""
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._started = False
            conversations = list(self._conversations.values())
            self._conversations.clear()

        await asyncio.gather(*(conversation.close() for conversation in conversations))


    async def create(
        self,
        knowledge_name: str | None = None,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self._operation_manager.run(
            OperationType.CONVERSATION_CREATE,
            lambda active_operation: self._create(
                knowledge_name,
                operation=active_operation,
            ),
            operation=operation,
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
                conversation_id = uuid4().hex[:12]
                conversation = Conversation(
                    self.conversation_dir / conversation_id,
                    operation_manager=self._operation_manager,
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
            return self._conversations[conversation_id]
        except KeyError as exc:
            raise RavenError(
                ErrorCode.CONVERSATION_NOT_FOUND,
                f"Conversation '{conversation_id}' does not exist.",
            ) from exc


    def list(self) -> list[dict[str, Any]]:
        """Return metadata for all opened conversations."""
        self._ensure_started()
        return [
            conversation.to_dict()
            for conversation in self._conversations.values()
        ]


    async def update(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self._operation_manager.run(
            OperationType.CONVERSATION_UPDATE_METADATA,
            lambda active_operation: self._update_metadata(
                conversation_id,
                title=title,
                pinned=pinned,
                operation=active_operation,
            ),
            operation=operation,
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
        return await self._operation_manager.run(
            OperationType.CONVERSATION_DELETE,
            lambda active_operation: self._delete(
                conversation_id,
                operation=active_operation,
            ),
            operation=operation,
        )


    async def _delete(
        self,
        conversation_id: str,
        *,
        operation: Operation,
    ) -> None:
        """Remove a conversation from the registry and disk."""
        conversation = self.get(conversation_id)
        await self._emit(
            operation,
            EventType.CONVERSATION_DELETE_STARTED,
            {"conversation_id": conversation.conversation_id},
        )

        try:
            async with self._mutation_lock:
                await conversation.close()
                await asyncio.to_thread(
                    shutil.rmtree,
                    conversation.dir_path,
                )
                self._conversations.pop(conversation.conversation_id, None)

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
        return [
            entry
            for entry in self.conversation_dir.iterdir()
            if entry.is_dir() and (entry / "metadata.json").is_file()
        ]


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
