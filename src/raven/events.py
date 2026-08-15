from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator
from uuid import uuid4

from pydantic import BaseModel, Field


class EventType(str, Enum):
    MODEL_PULL_PROGRESS = "model.pull.progress"
    MODEL_PULL_COMPLETE = "model.pull.complete"
    MODEL_LIST = "model.list"
    MODEL_INSPECT = "model.inspect"
    MODEL_DELETED = "model.deleted"
    MODEL_UNLOADED = "model.unloaded"

    OLLAMA_DOWNLOAD_PROGRESS = "ollama.download.progress"
    OLLAMA_DOWNLOAD_COMPLETE = "ollama.download.complete"

    INGESTION_PROGRESS = "ingestion.progress"
    INGESTION_COMPLETE = "ingestion.complete"

    KNOWLEDGE_CREATED = "knowledge.created"
    KNOWLEDGE_DELETED = "knowledge.deleted"
    KNOWLEDGE_UPDATED = "knowledge.updated"
    KNOWLEDGE_FILE_DELETED = "knowledge.file_deleted"
    KNOWLEDGE_FILE_INGESTED = "knowledge.file_ingested"

    RETRIEVAL_MODE_SELECTED = "retrieval.mode_selected"

    RETRIEVAL_EMBEDDED_STARTED = "retrieval.embedded.started"
    RETRIEVAL_EMBEDDED_COMPLETED = "retrieval.embedded.completed"

    RETRIEVAL_HIERARCHICAL_STARTED = "retrieval.hierarchical.started"
    RETRIEVAL_HIERARCHICAL_READ = "retrieval.hierarchical.read"
    RETRIEVAL_HIERARCHICAL_COMPLETED = "retrieval.hierarchical.completed"

    RETRIEVAL_AGREEMENT_STARTED = "retrieval.agreement.started"
    RETRIEVAL_AGREEMENT_COMPLETED = "retrieval.agreement.completed"

    RETRIEVAL_VECTOR_CONDITIONED_STARTED = "retrieval.vector_conditioned.started"
    RETRIEVAL_VECTOR_CONDITIONED_COMPLETED = "retrieval.vector_conditioned.completed"

    RECONSTRUCTION_STARTED = "reconstruction.started"
    RECONSTRUCTION_COMPLETED = "reconstruction.completed"

    CHAT_DELTA = "chat.delta"
    CHAT_TOOL_CALL = "chat.tool_call"
    CHAT_TOOL_RESULT = "chat.tool_result"
    CHAT_RETRIEVED = "chat.retrieved"
    CHAT_COMPLETE = "chat.complete"

    CONVERSATION_CREATED = "conversation.created"
    CONVERSATION_DELETED = "conversation.deleted"
    CONVERSATION_UPDATED = "conversation.updated"
    CONVERSATION_TITLE_GENERATED = "conversation.title_generated"

    ERROR = "error"
    LIFECYCLE_STARTED = "lifecycle.started"
    LIFECYCLE_STOPPED = "lifecycle.stopped"


class Event(BaseModel):
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)
    op_id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str | None = None
    ts: float = Field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class _Subscriber:
    queue: asyncio.Queue[Event | None]
    loop: asyncio.AbstractEventLoop
    op_id: str | None = None
    type: EventType | None = None

    def wants(self, event: Event) -> bool:
        if self.op_id is not None and event.op_id != self.op_id:
            return False
        if self.type is not None and event.type is not self.type:
            return False
        return True


class EventBus:
    def __init__(
        self,
        history_size: int = 500,
        queue_size: int = 200,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._subscribers: set[_Subscriber] = set()
        self._history: deque[Event] = deque(maxlen=history_size)
        self._queue_size = queue_size
        self._loop = loop
        self._closed = False

    def publish(self, event: Event) -> None:
        if self._closed:
            raise RuntimeError("bus is closed")

        self._history.append(event)

        # Iterate over a snapshot list to prevent set modification errors during iteration
        for sub in list(self._subscribers):
            if sub.wants(event):
                try:
                    sub.queue.put_nowait(event)
                except asyncio.QueueFull:
                    # Drop oldest unread item to make room for fresh event if consumer falls behind
                    try:
                        sub.queue.get_nowait()
                        sub.queue.put_nowait(event)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass

    def publish_from_thread(self, event: Event) -> None:
        if self._closed:
            raise RuntimeError("bus is closed")

        # Resolve main event loop instance
        loop = self._loop
        if loop is None:
            if self._subscribers:
                loop = next(iter(self._subscribers)).loop
            else:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

        if loop and loop.is_running():
            # Safely schedule publish call on the main asyncio event loop
            loop.call_soon_threadsafe(self.publish, event)
        else:
            # Fallback if loop isn't active yet
            self._history.append(event)

    def history(self, op_id: str | None = None, type: EventType | None = None) -> list[Event]:
        return [
            e
            for e in self._history
            if (op_id is None or e.op_id == op_id) and (type is None or e.type is type)
        ]

    def subscribe(
        self,
        op_id: str | None = None,
        type: EventType | None = None,
    ) -> AsyncIterator[Event]:
        return self._subscribe(op_id, type)

    async def _subscribe(
        self,
        op_id: str | None,
        type: EventType | None,
    ) -> AsyncIterator[Event]:
        loop = asyncio.get_running_loop()
        if self._closed:
            raise RuntimeError("bus is closed")
        if self._loop is None:
            self._loop = loop

        sub = _Subscriber(
            queue=asyncio.Queue(maxsize=self._queue_size),
            loop=loop,
            op_id=op_id,
            type=type,
        )
        self._subscribers.add(sub)
        try:
            while True:
                item = await sub.queue.get()
                if item is None:
                    return
                yield item
        finally:
            self._subscribers.discard(sub)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for sub in list(self._subscribers):
            try:
                sub.queue.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    sub.queue.get_nowait()
                    sub.queue.put_nowait(None)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass