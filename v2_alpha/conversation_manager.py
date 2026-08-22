"""Conversation persistence and lifecycle management for Raven."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import PathConfig
from .errors import ErrorCode, RavenError, error_payload
from .events import Event, EventType
from .knowledge_base import KnowledgeBase
from .operations import Operation


DEFAULT_TITLE = "New Conversation"
PERSISTENCE_VERSION = 1
_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class Conversation:
    """Durable identity and metadata for one conversation."""

    def __init__(
        self,
        dir_path: Path,
    ) -> None:
        self.dir_path = Path(dir_path)
        self.conversation_id = ""
        self.knowledge_name: str | None = None
        self.title = DEFAULT_TITLE
        self.pinned = False
        self.created_at: datetime | None = None

        self.metadata_path = self.dir_path / "metadata.json"
        self.messages_path = self.dir_path / "messages.json"
        self.facts_path = self.dir_path / "facts.json"
        self.context_path = self.dir_path / "context.json"
        self.qdrant_dir = self.dir_path / "qdrant"


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
        pinned: bool,
        created_at: datetime,
    ) -> None:
        """Set the complete conversation metadata state."""
        self.conversation_id = conversation_id
        self.knowledge_name = knowledge_name
        self.title = title
        self.pinned = pinned
        self.created_at = created_at


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


    @classmethod
    def from_dir(cls, dir_path: Path) -> "Conversation":
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

        knowledge_name = data.get("knowledge_name")
        if knowledge_name is not None and not isinstance(knowledge_name, str):
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "Conversation metadata contains an invalid knowledge_name.",
            )

        conversation = cls(Path(dir_path))
        conversation._set_metadata(
            conversation_id=conversation_id,
            knowledge_name=knowledge_name,
            title=title,
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
    ) -> None:
        self.conversation_dir = paths.conversations_dir
        self._knowledge_base = knowledge_base
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
                conversation = await asyncio.to_thread(Conversation.from_dir, path)
                loaded[conversation.conversation_id] = conversation

            self._conversations = loaded
            self._started = True


    async def close(self) -> None:
        """Close the manager and release its in-memory registry."""
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._started = False
            self._conversations.clear()


    async def create(
        self,
        knowledge_name: str | None = None,
        *,
        operation: Operation | None = None,
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
                conversation = Conversation(self.conversation_dir / conversation_id)
                conversation._set_metadata(
                    conversation_id=conversation_id,
                    knowledge_name=knowledge_name,
                    title=DEFAULT_TITLE,
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
        """Return one opened conversation."""
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
    ) -> Conversation:
        """Update mutable conversation metadata."""
        conversation = self.get(conversation_id)
        if title is None and pinned is None:
            return conversation
        if title is not None:
            title = self._validate_title(title)

        await self._emit(
            operation,
            EventType.CONVERSATION_UPDATE_STARTED,
            {"conversation_id": conversation.conversation_id},
        )

        try:
            async with self._mutation_lock:
                created_at = conversation.created_at
                if created_at is None:
                    raise RavenError(
                        ErrorCode.INVALID_METADATA,
                        "Conversation metadata has not been initialized.",
                    )
                conversation._set_metadata(
                    conversation_id=conversation.conversation_id,
                    knowledge_name=conversation.knowledge_name,
                    title=title if title is not None else conversation.title,
                    pinned=pinned if pinned is not None else conversation.pinned,
                    created_at=created_at,
                )
                await asyncio.to_thread(conversation.write_metadata)

            await self._emit(
                operation,
                EventType.CONVERSATION_UPDATE_COMPLETED,
                conversation.to_dict(),
            )
            return conversation
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                operation,
                EventType.CONVERSATION_UPDATE_FAILED,
                {
                    "conversation_id": conversation.conversation_id,
                    "error": error_payload(exc),
                },
            )
            raise


    async def delete(
        self,
        conversation_id: str,
        *,
        operation: Operation | None = None,
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
                await asyncio.to_thread(
                    shutil.rmtree,
                    conversation.dir_path,
                    ignore_errors=True,
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


    @staticmethod
    def _validate_title(title: str) -> str:
        if not isinstance(title, str) or not title.strip():
            raise RavenError(
                ErrorCode.INVALID_CONVERSATION_TITLE,
                "Conversation title must be a non-empty string.",
            )
        return title.strip()


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
