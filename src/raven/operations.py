"""Operation tracking for long-running Raven work.

The domain core still emits ordinary :class:`Event` objects.  This module
owns task creation and exposes a small, transport-neutral operation registry
that the FastAPI adapter can turn into REST and SSE.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .events import Event, EventBus, EventType


class OperationStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


DEFAULT_TERMINAL_EVENTS = frozenset(
    {
        EventType.OPERATION_COMPLETED,
        EventType.OPERATION_CANCELLED,
    }
)


@dataclass(slots=True)
class OperationRecord:
    operation_id: str
    status: OperationStatus = OperationStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: Any = None
    error: str | None = None
    task: asyncio.Task[Any] | None = None
    terminal_events: frozenset[EventType] = DEFAULT_TERMINAL_EVENTS
    event_history: deque[Event] = field(default_factory=deque)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


class OperationManager:
    """Own tasks and correlate domain events with operation state."""

    def __init__(self, bus: EventBus, max_records: int = 1000, history_size: int = 500) -> None:
        self.bus = bus
        self.max_records = max_records
        self.history_size = history_size
        self._records: dict[str, OperationRecord] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self.bus.add_listener(self._on_event)

    def _on_event(self, event: Event) -> None:
        record = self._records.get(event.op_id)
        if record is None:
            return
        if len(record.event_history) >= self.history_size:
            record.event_history.popleft()
        record.event_history.append(event)
        if event.type is EventType.ERROR:
            record.status = OperationStatus.FAILED
            record.error = str(event.data.get("error", "operation failed"))
            record.finished_at = time.time()
        elif event.type is EventType.OPERATION_CANCELLED:
            record.status = OperationStatus.CANCELLED
            record.finished_at = time.time()
        elif event.type in record.terminal_events:
            record.status = OperationStatus.COMPLETED
            record.finished_at = time.time()

    async def submit(
        self,
        work: Callable[[str], Awaitable[Any]],
        *,
        operation_id: str | None = None,
        terminal_events: frozenset[EventType] | None = None,
    ) -> OperationRecord:
        if self._closed:
            raise RuntimeError("operation manager is closed")
        op_id = operation_id or uuid4().hex
        async with self._lock:
            terminal_ids = [
                operation_id
                for operation_id, item in self._records.items()
                if item.status in {
                    OperationStatus.COMPLETED,
                    OperationStatus.FAILED,
                    OperationStatus.CANCELLED,
                }
            ]
            while len(self._records) >= self.max_records and terminal_ids:
                del self._records[terminal_ids.pop(0)]
            if op_id in self._records:
                raise ValueError(f"operation '{op_id}' already exists")
            record = OperationRecord(
                operation_id=op_id,
                terminal_events=terminal_events or DEFAULT_TERMINAL_EVENTS,
            )
            self._records[op_id] = record
            self.bus.publish(
                Event(
                    type=EventType.OPERATION_STARTED,
                    data={"operation_id": op_id},
                    op_id=op_id,
                )
            )
            record.task = asyncio.create_task(self._run(record, work))
            return record

    async def _run(self, record: OperationRecord, work: Callable[[str], Awaitable[Any]]) -> None:
        record.status = OperationStatus.RUNNING
        record.started_at = time.time()
        try:
            record.result = await work(record.operation_id)
            if record.status in (OperationStatus.QUEUED, OperationStatus.RUNNING):
                self.bus.publish(
                    Event(
                        type=EventType.OPERATION_COMPLETED,
                        data={"operation_id": record.operation_id, "result": record.result},
                        op_id=record.operation_id,
                    )
                )
        except asyncio.CancelledError:
            record.status = OperationStatus.CANCELLED
            record.finished_at = time.time()
            self.bus.publish(
                Event(
                    type=EventType.OPERATION_CANCELLED,
                    data={"operation_id": record.operation_id},
                    op_id=record.operation_id,
                )
            )
        except Exception as exc:
            record.status = OperationStatus.FAILED
            record.error = str(exc)
            record.finished_at = time.time()
            self.bus.publish(
                Event(
                    type=EventType.ERROR,
                    data={"op": "operation", "error": str(exc)},
                    op_id=record.operation_id,
                )
            )
        finally:
            if record.finished_at is None and record.status in {
                OperationStatus.COMPLETED,
                OperationStatus.FAILED,
                OperationStatus.CANCELLED,
            }:
                record.finished_at = time.time()

    def get(self, operation_id: str) -> OperationRecord:
        try:
            return self._records[operation_id]
        except KeyError:
            raise KeyError(f"operation '{operation_id}' does not exist") from None

    async def wait(self, operation_id: str) -> OperationRecord:
        record = self.get(operation_id)
        if record.task is not None:
            await asyncio.shield(record.task)
        return record

    async def cancel(self, operation_id: str) -> OperationRecord:
        record = self.get(operation_id)
        if record.task is not None and not record.task.done():
            record.task.cancel()
            await asyncio.gather(record.task, return_exceptions=True)
        if record.status in (OperationStatus.QUEUED, OperationStatus.RUNNING):
            record.status = OperationStatus.CANCELLED
            record.finished_at = time.time()
            self.bus.publish(
                Event(
                    type=EventType.OPERATION_CANCELLED,
                    data={"operation_id": record.operation_id},
                    op_id=record.operation_id,
                )
            )
        return record

    def history(self, operation_id: str, after_event_id: int = 0) -> list[Event]:
        record = self.get(operation_id)
        return [
            event
            for event in record.event_history
            if event.event_id is None or event.event_id > after_event_id
        ]

    def history_bounds(self, operation_id: str) -> tuple[int | None, int | None]:
        record = self.get(operation_id)
        values = [event.event_id for event in record.event_history if event.event_id is not None]
        return (min(values), max(values)) if values else (None, None)

    async def close(self) -> None:
        self._closed = True
        tasks = [r.task for r in self._records.values() if r.task and not r.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.bus.remove_listener(self._on_event)
