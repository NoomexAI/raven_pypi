from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ._paths import data_root
from .events import Event, EventBus, EventType
from .knowledge import KnowledgeBase

DEFAULT_TITLE = "New Conversation"
PERSISTENCE_VERSION = 1


class Conversation:
    """Identity + on-disk persistence for one chat conversation.

    Mirrors the Knowledge pattern: a Conversation never knows its storage
    location — the ConversationManager decides that. On-disk layout:
    conversations/<id>/{metadata.json, messages.json, context.json, qdrant/}
    """

    def __init__(
        self,
        conversation_id: str,
        dir_path: Path,
        knowledge_name: str | None = None,
        title: str = DEFAULT_TITLE,
        pinned: bool = False,
        created_at: str | None = None,
    ) -> None:
        self.conversation_id = conversation_id
        self.dir_path = Path(dir_path)
        self.knowledge_name = knowledge_name
        self.type = "local" if knowledge_name else "global"
        self.title = title
        self.pinned = pinned
        self.created_at = created_at or datetime.now().isoformat()
        self.metadata_path = self.dir_path / "metadata.json"
        self.messages_path = self.dir_path / "messages.json"
        self.context_path = self.dir_path / "context.json"
        self.qdrant_dir = self.dir_path / "qdrant"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PERSISTENCE_VERSION,
            "conversation_id": self.conversation_id,
            "type": self.type,
            "knowledge_name": self.knowledge_name,
            "title": self.title,
            "pinned": self.pinned,
            "created_at": self.created_at,
        }

    def write_metadata(self) -> None:
        self.dir_path.mkdir(parents=True, exist_ok=True)
        self.metadata_path.write_text(
            json.dumps(self.to_dict(), indent=4, ensure_ascii=False),
            encoding="utf-8",
        )

    def update_title(self, title: str) -> None:
        self.title = title
        self.write_metadata()

    def toggle_pin(self) -> bool:
        self.pinned = not self.pinned
        self.write_metadata()
        return self.pinned

    @classmethod
    def from_dir(cls, dir_path: Path) -> "Conversation":
        meta = json.loads((dir_path / "metadata.json").read_text(encoding="utf-8"))
        return cls(
            conversation_id=meta["conversation_id"],
            dir_path=dir_path,
            knowledge_name=meta.get("knowledge_name"),
            title=meta.get("title", DEFAULT_TITLE),
            pinned=meta.get("pinned", False),
            created_at=meta.get("created_at"),
        )


class ConversationManager:
    """Manager/registry of Conversation directories.

    Sole owner of the conversations base path (default <project>/data/conversations
    or $RAVEN_DATA_DIR / $RAVEN_HOME / conversations). Mirrors KnowledgeBase:
    construction is config-only; ``await start()`` scans existing dirs.
    """

    def __init__(
        self,
        bus: EventBus,
        conversation_dir: Path | None = None,
        knowledge_base: KnowledgeBase | None = None,
    ) -> None:
        self.bus = bus
        self.knowledge_base = knowledge_base
        self.conversation_dir = self._resolve_base_path(conversation_dir)
        self.conversation_dir.mkdir(parents=True, exist_ok=True)
        self._conversations: dict[str, Conversation] = {}
        self._started = False

    @staticmethod
    def _resolve_base_path(conversation_dir: Path | None) -> Path:
        if conversation_dir is not None:
            return Path(conversation_dir).resolve()
        for var in ("RAVEN_DATA_DIR", "RAVEN_HOME"):
            val = os.environ.get(var)
            if val:
                return Path(val).resolve() / "conversations"
        return data_root() / "conversations"

    def _scan_existing(self) -> list[str]:
        result = []
        if not self.conversation_dir.is_dir():
            return result
        for entry in self.conversation_dir.iterdir():
            if not entry.is_dir():
                continue
            if not (entry / "metadata.json").exists():
                continue
            try:
                meta = json.loads((entry / "metadata.json").read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(meta, dict) and "conversation_id" in meta:
                result.append(meta["conversation_id"])
        return result

    async def start(self) -> None:
        if self._started:
            return
        ids = await asyncio.to_thread(self._scan_existing)
        for cid in ids:
            if cid in self._conversations:
                continue
            self._conversations[cid] = await asyncio.to_thread(
                Conversation.from_dir, self.conversation_dir / cid
            )
        self._started = True
        self.bus.publish(
            Event(
                type=EventType.LIFECYCLE_STARTED,
                data={"conversation_dir": str(self.conversation_dir)},
            )
        )

    async def create(self, knowledge_name: str | None = None) -> Conversation:
        if knowledge_name and self.knowledge_base is not None:
            try:
                self.knowledge_base.get(knowledge_name)
            except KeyError:
                raise ValueError(f"knowledge '{knowledge_name}' does not exist")
        conversation_id = uuid4().hex[:12]
        dir_path = self.conversation_dir / conversation_id
        conversation = await asyncio.to_thread(
            Conversation,
            conversation_id=conversation_id,
            dir_path=dir_path,
            knowledge_name=knowledge_name,
        )
        await asyncio.to_thread(conversation.write_metadata)
        self._conversations[conversation_id] = conversation
        self.bus.publish(
            Event(
                type=EventType.CONVERSATION_CREATED,
                data=conversation.to_dict(),
            )
        )
        return conversation

    def get(self, conversation_id: str) -> Conversation:
        if conversation_id not in self._conversations:
            raise KeyError(f"conversation '{conversation_id}' does not exist")
        return self._conversations[conversation_id]

    def list(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self._conversations.values()]

    async def delete(self, conversation_id: str) -> None:
        if conversation_id not in self._conversations:
            raise KeyError(f"conversation '{conversation_id}' does not exist")
        dir_path = self._conversations[conversation_id].dir_path
        del self._conversations[conversation_id]
        await asyncio.to_thread(
            lambda: __import__("shutil").rmtree(dir_path, ignore_errors=True)
        )
        self.bus.publish(
            Event(
                type=EventType.CONVERSATION_DELETED,
                data={"conversation_id": conversation_id},
            )
        )

    async def rename(self, conversation_id: str, title: str) -> Conversation:
        conversation = self.get(conversation_id)
        if title:
            await asyncio.to_thread(conversation.update_title, title)
            self.bus.publish(
                Event(
                    type=EventType.CONVERSATION_UPDATED,
                    data=conversation.to_dict(),
                )
            )
        return conversation

    async def toggle_pin(self, conversation_id: str) -> Conversation:
        conversation = self.get(conversation_id)
        await asyncio.to_thread(conversation.toggle_pin)
        self.bus.publish(
            Event(
                type=EventType.CONVERSATION_UPDATED,
                data=conversation.to_dict(),
            )
        )
        return conversation
