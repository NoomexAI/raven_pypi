"""Application-level operation management for Raven."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from .events import Event, EventStream, EventStreamRegistry



class OperationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"



class OperationContext:
    """The scoped interface a worker uses for one operation."""

    def __init__(
        self,
        operation_id: UUID,
        stream: EventStream,
        is_cancelled: Callable[[], bool],
    ) -> None:
        self.operation_id = operation_id
        self._stream = stream
        self._is_cancelled = is_cancelled

    @property
    def cancelled(self) -> bool:
        return self._is_cancelled()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError

    async def publish(self, event: Event) -> Event:
        if event.is_final:
            raise ValueError("workers cannot publish final operation events")
        if event.operation_id is not None and event.operation_id != self.operation_id:
            raise ValueError("event operation_id does not match the operation context")
        return await self._stream.publish(
            event.model_copy(update={"operation_id": self.operation_id})
        )



class OperationSnapshot(BaseModel):
    """A public, immutable view of operation state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: UUID
    name: str
    status: OperationStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: Any = None
    error: str | None = None
    cancellation_requested: bool = False



OperationWorker = Callable[[OperationContext], Awaitable[Any]]
_TERMINAL_STATUSES = frozenset(
    {
        OperationStatus.COMPLETED,
        OperationStatus.FAILED,
        OperationStatus.CANCELLED,
    }
)


@dataclass(slots=True)
class _OperationState:
    operation_id: UUID
    name: str
    stream: EventStream
    created_at: datetime
    status: OperationStatus = OperationStatus.QUEUED
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: Any = None
    error: str | None = None
    cancellation_requested: bool = False
    task: asyncio.Task[Any] | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)



class OperationManager:
    """Create, track, cancel, and observe Raven operations."""

    def __init__(self, registry: EventStreamRegistry) -> None:
        self._registry = registry
        self._operations: dict[str, _OperationState] = {}
        self._lock = asyncio.Lock()
        self._closed = False


    async def submit(
        self,
        name: str,
        worker: OperationWorker,
        *,
        operation_id: UUID | str | None = None,
    ) -> OperationSnapshot:
        """Start a worker under a new operation and return its initial state."""
        if not name.strip():
            raise ValueError("operation name cannot be empty")

        parsed_id = self._parse_operation_id(operation_id) if operation_id is not None else uuid4()
        key = str(parsed_id)

        async with self._lock:
            self._ensure_open()
            if key in self._operations or key in self._registry.stored_operation_ids():
                raise ValueError(f"operation '{key}' already exists")

            stream = self._registry.get(key)
            state = _OperationState(
                operation_id=parsed_id,
                name=name,
                stream=stream,
                created_at=datetime.now(timezone.utc),
            )
            self._operations[key] = state
            await stream.publish(
                Event(
                    type="operation.queued",
                    data={"name": name},
                )
            )
            state.task = asyncio.create_task(
                self._run(state, worker),
                name=f"raven-operation-{key}",
            )
            return self._snapshot(state)


    async def get(self, operation_id: UUID | str) -> OperationSnapshot:
        """Return active state or reconstruct state from a persisted event log."""
        parsed_id = self._parse_operation_id(operation_id)
        key = str(parsed_id)

        async with self._lock:
            state = self._operations.get(key)
            if state is not None:
                return self._snapshot(state)

        if key not in self._registry.stored_operation_ids():
            raise KeyError(f"operation '{key}' was not found")
        events = await self._registry.get(key).read()
        return self._snapshot_from_events(parsed_id, events)


    async def wait(self, operation_id: UUID | str) -> OperationSnapshot:
        """Wait for an active operation, then return its final state."""
        parsed_id = self._parse_operation_id(operation_id)
        key = str(parsed_id)

        async with self._lock:
            state = self._operations.get(key)
            task = state.task if state is not None else None

        if state is None:
            return await self.get(parsed_id)

        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return await self.get(parsed_id)


    async def cancel(self, operation_id: UUID | str) -> OperationSnapshot:
        """Request cancellation of an active operation."""
        parsed_id = self._parse_operation_id(operation_id)
        key = str(parsed_id)
        
        async with self._lock:
            state = self._operations.get(key)
            if state is None:
                raise KeyError(f"operation '{key}' was not found")
            if state.status in _TERMINAL_STATUSES:
                return self._snapshot(state)
            state.cancellation_requested = True
            task = state.task
            snapshot = self._snapshot(state)

        if task is not None and not task.done():
            task.cancel()
        return snapshot


    async def events(
        self,
        operation_id: UUID | str,
        *,
        after_event_id: int = 0,
    ) -> AsyncIterator[Event]:
        """Replay and follow events for one operation."""
        parsed_id = self._parse_operation_id(operation_id)
        key = str(parsed_id)

        async with self._lock:
            known = key in self._operations
        if not known and key not in self._registry.stored_operation_ids():
            raise KeyError(f"operation '{key}' was not found")

        stream = self._registry.get(key)
        async for event in stream.events(after_event_id=after_event_id):
            yield event


    async def close(self) -> None:
        """Cancel active operations and close the event-stream registry."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            active = [
                state
                for state in self._operations.values()
                if state.status not in _TERMINAL_STATUSES
            ]
            tasks = []
            for state in active:
                state.cancellation_requested = True
                if state.task is not None and not state.task.done():
                    state.task.cancel()
                    tasks.append(state.task)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._registry.close()


    async def _run(self, state: _OperationState, worker: OperationWorker) -> None:
        try:
            await self._mark_running(state)
            context = OperationContext(
                operation_id=state.operation_id,
                stream=state.stream,
                is_cancelled=lambda: state.cancellation_requested,
            )
            result = await worker(context)
            if state.cancellation_requested:
                await self._finish_cancelled(state)
            else:
                await self._finish_completed(state, result)
        except asyncio.CancelledError:
            await self._finish_cancelled(state)
        except Exception as exc:
            await self._finish_failed(state, exc)
        finally:
            state.done.set()


    async def _mark_running(self, state: _OperationState) -> None:
        async with self._lock:
            if state.cancellation_requested:
                raise asyncio.CancelledError
            state.status = OperationStatus.RUNNING
            state.started_at = datetime.now(timezone.utc)
        await state.stream.publish(
            Event(
                type="operation.started",
                data={"name": state.name},
            )
        )


    async def _finish_completed(self, state: _OperationState, result: Any) -> None:
        await self._finish(
            state,
            status=OperationStatus.COMPLETED,
            event_type="operation.completed",
            result=result,
        )


    async def _finish_failed(self, state: _OperationState, error: BaseException) -> None:
        await self._finish(
            state,
            status=OperationStatus.FAILED,
            event_type="operation.failed",
            error=str(error) or error.__class__.__name__,
        )


    async def _finish_cancelled(self, state: _OperationState) -> None:
        await self._finish(
            state,
            status=OperationStatus.CANCELLED,
            event_type="operation.cancelled",
        )


    async def _finish(
        self,
        state: _OperationState,
        *,
        status: OperationStatus,
        event_type: str,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        async with self._lock:
            if state.status in _TERMINAL_STATUSES:
                return
            state.status = status
            state.finished_at = datetime.now(timezone.utc)
            state.result = result
            state.error = error

        data: dict[str, Any] = {"name": state.name}
        if error is not None:
            data["error"] = error
        await state.stream.publish(Event(type=event_type, data=data, is_final=True))


    def _snapshot(self, state: _OperationState) -> OperationSnapshot:
        return OperationSnapshot(
            operation_id=state.operation_id,
            name=state.name,
            status=state.status,
            created_at=state.created_at,
            started_at=state.started_at,
            finished_at=state.finished_at,
            result=state.result,
            error=state.error,
            cancellation_requested=state.cancellation_requested,
        )


    @staticmethod
    def _snapshot_from_events(operation_id: UUID, events: list[Event]) -> OperationSnapshot:
        if not events:
            raise KeyError(f"operation '{operation_id}' was not found")

        queued = next((event for event in events if event.type == "operation.queued"), None)
        started = next((event for event in events if event.type == "operation.started"), None)
        final = next((event for event in events if event.is_final), None)
        status = OperationStatus.RUNNING
        error = None
        finished_at = None
        if final is not None:
            finished_at = final.timestamp
            if final.type == "operation.completed":
                status = OperationStatus.COMPLETED
            elif final.type == "operation.cancelled":
                status = OperationStatus.CANCELLED
            elif final.type == "operation.failed":
                status = OperationStatus.FAILED
                error = final.data.get("error")

        name = queued.data.get("name") if queued else None
        if not isinstance(name, str) or not name:
            name = "unknown"

        return OperationSnapshot(
            operation_id=operation_id,
            name=name,
            status=status,
            created_at=events[0].timestamp,
            started_at=started.timestamp if started else None,
            finished_at=finished_at,
            error=error,
        )


    @staticmethod
    def _parse_operation_id(operation_id: UUID | str) -> UUID:
        try:
            parsed = operation_id if isinstance(operation_id, UUID) else UUID(operation_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("operation_id must be a valid UUID") from exc
        return parsed


    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("operation manager is closed")
