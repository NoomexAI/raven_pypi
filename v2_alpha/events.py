"""Persistent per-operation event streams for Raven."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Event(BaseModel):
    """An event stored in an operation's event stream."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    operation_id: UUID | None = None
    event_id: int | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_final: bool = False


class EventStream:
    """The persistent event stream belonging to one operation."""

    def __init__(self, operation_id: str, path: Path) -> None:
        self.operation_id = operation_id
        self._path = path
        self._condition = asyncio.Condition()
        self._last_event_id = 0
        self._finished = False
        self._finished_at: datetime | None = None
        self._loaded = False
        self._closed = False


    async def publish(self, event: Event) -> Event:
        """Persist an event, then wake subscribers to this stream."""
        self._ensure_open()

        async with self._condition:
            await self._load()
            if self._finished:
                raise RuntimeError(f"operation '{self.operation_id}' is already finished")

            persisted = event.model_copy(
                update={
                    "operation_id": UUID(self.operation_id),
                    "event_id": self._last_event_id + 1,
                }
            )
            await asyncio.to_thread(self._append, persisted)

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
            return await asyncio.to_thread(self._read_after, after_event_id)


    async def events(self, after_event_id: int = 0) -> AsyncIterator[Event]:
        """Yield events after a cursor, then wait for future stream events."""
        self._validate_cursor(after_event_id)
        cursor = after_event_id

        while True:
            async with self._condition:
                await self._load()
                events = await asyncio.to_thread(self._read_after, cursor)

                if not events:
                    if self._finished or self._closed:
                        return
                    await self._condition.wait()
                    continue

            for event in events:
                cursor = event.event_id or cursor
                yield event


    async def delete(self) -> None:
        """Delete this stream's persisted event log and wake subscribers."""
        async with self._condition:
            await self._load()
            await asyncio.to_thread(self._path.unlink, missing_ok=True)
            self._finished = True
            self._closed = True
            self._condition.notify_all()


    async def is_expired_before(self, timestamp: datetime) -> bool:
        """Return whether a finished stream predates ``timestamp``."""
        async with self._condition:
            await self._load()
            return self._finished and self._finished_at is not None and self._finished_at < timestamp


    async def close(self) -> None:
        """Stop live activity without deleting the persisted event log."""
        if self._closed:
            return
        self._closed = True
        async with self._condition:
            self._condition.notify_all()


    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("event stream is closed")


    @staticmethod
    def _validate_cursor(after_event_id: int) -> None:
        if after_event_id < 0:
            raise ValueError("after_event_id cannot be negative")


    async def _load(self) -> None:
        if self._loaded:
            return
        self._last_event_id, self._finished, self._finished_at = await asyncio.to_thread(
            self._read_metadata
        )
        self._loaded = True


    def _append(self, event: Event) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event.model_dump(mode="json"), separators=(",", ":")) + "\n")
            stream.flush()


    def _read_metadata(self) -> tuple[int, bool, datetime | None]:
        if not self._path.exists():
            return 0, False, None

        last_event_id = 0
        finished = False
        finished_at: datetime | None = None
        with self._path.open("r", encoding="utf-8") as stream:
            for line in stream:
                record = self._decode_line(line)
                if record is None:
                    break
                event = Event.model_validate(record)
                if event.event_id is None:
                    raise RuntimeError("event log entry is missing event_id")
                last_event_id = event.event_id
                finished = event.is_final
                if finished:
                    finished_at = event.timestamp
        return last_event_id, finished, finished_at


    def _read_after(self, after_event_id: int) -> list[Event]:
        if not self._path.exists():
            return []

        events: list[Event] = []
        with self._path.open("r", encoding="utf-8") as stream:
            for line in stream:
                record = self._decode_line(line)
                if record is None:
                    break
                if int(record["event_id"]) > after_event_id:
                    events.append(Event.model_validate(record))
        return events


    @staticmethod
    def _decode_line(line: str) -> dict[str, Any] | None:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if not line.endswith("\n"):
                return None
            raise RuntimeError("event log contains invalid JSON")
        if not isinstance(value, dict):
            raise RuntimeError("event log entry must be a JSON object")
        return value



class EventStreamRegistry:
    """Create and retrieve operation-scoped event streams."""

    def __init__(self, storage_dir: str | Path) -> None:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._streams: dict[str, EventStream] = {}
        self._closed = False


    def get(self, operation_id: str) -> EventStream:
        """Return the stream for an operation, reopening it from disk if needed."""
        self._ensure_open()
        self._validate_operation_id(operation_id)
        stream = self._streams.get(operation_id)
        if stream is None:
            stream = EventStream(
                operation_id=operation_id,
                path=self.storage_dir / f"{operation_id}.jsonl",
            )
            self._streams[operation_id] = stream
        return stream


    def stored_operation_ids(self) -> list[str]:
        """Return UUIDs for persisted operation logs in this registry."""
        operation_ids: list[str] = []
        for path in self.storage_dir.glob("*.jsonl"):
            operation_id = path.stem
            try:
                self._validate_operation_id(operation_id)
            except ValueError:
                continue
            operation_ids.append(operation_id)
        return operation_ids


    async def delete(self, operation_id: str) -> None:
        stream = self.get(operation_id)
        await stream.delete()
        self._streams.pop(operation_id, None)


    async def close(self) -> None:
        """Close all active streams while preserving their event logs."""
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(*(stream.close() for stream in self._streams.values()))


    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("event stream registry is closed")


    @staticmethod
    def _validate_operation_id(operation_id: str) -> None:
        try:
            parsed = UUID(operation_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("operation_id must be a valid UUID") from exc

        if operation_id not in {parsed.hex, str(parsed)}:
            raise ValueError("operation_id must use the canonical UUID format")



class EventCleanupService:
    """Delete finished event streams after a configured retention period."""

    def __init__(self, registry: EventStreamRegistry, retention: timedelta) -> None:
        if retention < timedelta(0):
            raise ValueError("retention cannot be negative")
        self._registry = registry
        self.retention = retention


    async def run_once(self, now: datetime | None = None) -> list[str]:
        """Delete expired finished streams and return their operation IDs."""
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        cutoff = current_time.astimezone(timezone.utc) - self.retention
        deleted: list[str] = []

        for operation_id in self._registry.stored_operation_ids():
            stream = self._registry.get(operation_id)
            if await stream.is_expired_before(cutoff):
                await self._registry.delete(operation_id)
                deleted.append(operation_id)

        return deleted
