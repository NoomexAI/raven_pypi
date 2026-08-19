"""Application-level operation management for Raven."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from .events import Event, EventStream, EventStreamRegistry, EventType



class OperationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"



OperationWorker = Callable[["Operation"], Awaitable[Any]]
_TERMINAL_STATUSES = frozenset(
    {
        OperationStatus.COMPLETED,
        OperationStatus.FAILED,
        OperationStatus.CANCELLED,
    }
)



class Operation:
    """One Raven operation and its complete runtime lifecycle."""

    def __init__(
        self,
        operation_id: UUID,
        name: str,
        stream: EventStream,
        *,
        created_at: datetime | None = None,
    ) -> None:
        self.operation_id = operation_id
        self.name = name
        self._stream = stream
        self._created_at = created_at or datetime.now(timezone.utc)
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._status = OperationStatus.QUEUED
        self._result: Any = None
        self._error: str | None = None
        self._cancellation_requested = False
        self._task: asyncio.Task[Any] | None = None
        self._lock = asyncio.Lock()


    @property
    def status(self) -> OperationStatus:
        return self._status


    @property
    def created_at(self) -> datetime:
        return self._created_at


    @property
    def started_at(self) -> datetime | None:
        return self._started_at


    @property
    def finished_at(self) -> datetime | None:
        return self._finished_at


    @property
    def result(self) -> Any:
        return self._result


    @property
    def error(self) -> str | None:
        return self._error


    @property
    def cancellation_requested(self) -> bool:
        return self._cancellation_requested


    @property
    def is_finished(self) -> bool:
        return self._status in _TERMINAL_STATUSES


    async def start(self, worker: OperationWorker) -> None:
        """Queue this operation and start its worker task."""
        async with self._lock:
            if self._task is not None:
                raise RuntimeError(f"operation '{self.operation_id}' has already started")

            await self._stream.publish(
                Event(
                    type=EventType.OPERATION_QUEUED,
                    data={"name": self.name},
                )
            )
            self._task = asyncio.create_task(
                self._run(worker),
                name=f"raven-operation-{self.operation_id}",
            )


    async def wait(self) -> "Operation":
        """Wait for this operation's worker task and return this operation."""
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return self


    async def cancel(self) -> "Operation":
        """Request cancellation and cancel the worker task if it is active."""
        async with self._lock:
            if self.is_finished:
                return self

            self._cancellation_requested = True
            task = self._task

        if task is not None and not task.done():
            task.cancel()
        elif task is None:
            await self._finish_cancelled()
        return self


    async def publish(self, event: Event) -> Event:
        """Publish a domain event belonging to this operation."""
        if self.is_finished:
            raise RuntimeError(f"operation '{self.operation_id}' is already finished")
        if event.is_final:
            raise ValueError("workers cannot publish final operation events")
        if event.operation_id is not None and event.operation_id != self.operation_id:
            raise ValueError("event operation_id does not match the operation")

        return await self._stream.publish(
            event.model_copy(update={"operation_id": self.operation_id})
        )


    def raise_if_cancelled(self) -> None:
        if self._cancellation_requested:
            raise asyncio.CancelledError


    async def events(self, after_event_id: int = 0) -> AsyncIterator[Event]:
        async for event in self._stream.events(after_event_id=after_event_id):
            yield event


    @classmethod
    def from_events(
        cls,
        operation_id: UUID,
        stream: EventStream,
        events: list[Event],
    ) -> "Operation":
        """Reconstruct an operation from its persisted event history."""
        if not events:
            raise KeyError(f"operation '{operation_id}' has no persisted events")

        queued = next((event for event in events if event.type == EventType.OPERATION_QUEUED), None)
        name = queued.data.get("name") if queued else None
        if not isinstance(name, str) or not name:
            name = "unknown"

        operation = cls(
            operation_id=operation_id,
            name=name,
            stream=stream,
            created_at=events[0].timestamp,
        )
        operation._restore(events)
        return operation


    async def _run(self, worker: OperationWorker) -> None:
        try:
            await self._mark_running()
            result = await worker(self)
            if self._cancellation_requested:
                await self._finish_cancelled()
            else:
                await self._finish_completed(result)
        except asyncio.CancelledError:
            await self._finish_cancelled()
        except Exception as exc:
            await self._finish_failed(exc)


    async def _mark_running(self) -> None:
        async with self._lock:
            if self._cancellation_requested:
                raise asyncio.CancelledError

            self._status = OperationStatus.RUNNING
            self._started_at = datetime.now(timezone.utc)

        await self._stream.publish(
            Event(
                type=EventType.OPERATION_STARTED,
                data={"name": self.name},
            )
        )


    async def _finish_completed(self, result: Any) -> None:
        await self._finish(
            status=OperationStatus.COMPLETED,
            event_type=EventType.OPERATION_COMPLETED,
            result=result,
        )


    async def _finish_failed(self, error: BaseException) -> None:
        await self._finish(
            status=OperationStatus.FAILED,
            event_type=EventType.OPERATION_FAILED,
            error=str(error) or error.__class__.__name__,
        )


    async def _finish_cancelled(self) -> None:
        await self._finish(
            status=OperationStatus.CANCELLED,
            event_type=EventType.OPERATION_CANCELLED,
        )


    async def _finish(
        self,
        *,
        status: OperationStatus,
        event_type: EventType,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        async with self._lock:
            if self.is_finished:
                return

            self._status = status
            self._finished_at = datetime.now(timezone.utc)
            self._result = result
            self._error = error

        data: dict[str, Any] = {"name": self.name}
        if error is not None:
            data["error"] = error
        await self._stream.publish(
            Event(
                type=event_type,
                data=data,
                is_final=True,
            )
        )


    def _restore(self, events: list[Event]) -> None:
        started = next((event for event in events if event.type == EventType.OPERATION_STARTED), None)
        final = next((event for event in events if event.is_final), None)

        if started is not None:
            self._status = OperationStatus.RUNNING
            self._started_at = started.timestamp

        if final is None:
            return

        self._finished_at = final.timestamp
        if final.type == EventType.OPERATION_COMPLETED:
            self._status = OperationStatus.COMPLETED
        elif final.type == EventType.OPERATION_FAILED:
            self._status = OperationStatus.FAILED
            self._error = final.data.get("error")
        elif final.type == EventType.OPERATION_CANCELLED:
            self._status = OperationStatus.CANCELLED



class OperationManager:
    """Create, find, cancel, and shut down Raven operations."""

    def __init__(self, registry: EventStreamRegistry) -> None:
        self._registry = registry
        self._operations: dict[str, Operation] = {}
        self._lock = asyncio.Lock()
        self._closed = False


    async def submit(
        self,
        name: str,
        worker: OperationWorker,
    ) -> Operation:
        """Create a new operation with a generated ID and start its worker."""
        if not name.strip():
            raise ValueError("operation name cannot be empty")

        operation_id = uuid4()
        key = str(operation_id)

        async with self._lock:
            self._ensure_open()
            if key in self._operations or key in self._registry.stored_operation_ids():
                raise ValueError(f"operation '{key}' already exists")

            operation = Operation(
                operation_id=operation_id,
                name=name,
                stream=self._registry.get(key),
            )
            self._operations[key] = operation
            await operation.start(worker)
            return operation


    async def get(self, operation_id: UUID | str) -> Operation:
        """Return an active operation or reconstruct one from its event log."""
        parsed_id = self._parse_operation_id(operation_id)
        key = str(parsed_id)

        async with self._lock:
            operation = self._operations.get(key)
            if operation is not None:
                return operation

        if key not in self._registry.stored_operation_ids():
            raise KeyError(f"operation '{key}' was not found")

        stream = self._registry.get(key)
        operation = Operation.from_events(
            operation_id=parsed_id,
            stream=stream,
            events=await stream.read(),
        )

        async with self._lock:
            existing = self._operations.setdefault(key, operation)
            return existing


    async def wait(self, operation_id: UUID | str) -> Operation:
        operation = await self.get(operation_id)
        return await operation.wait()


    async def cancel(self, operation_id: UUID | str) -> Operation:
        operation = await self.get(operation_id)
        return await operation.cancel()


    async def events(
        self,
        operation_id: UUID | str,
        *,
        after_event_id: int = 0,
    ) -> AsyncIterator[Event]:
        operation = await self.get(operation_id)
        async for event in operation.events(after_event_id=after_event_id):
            yield event


    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            operations = [
                operation
                for operation in self._operations.values()
                if not operation.is_finished
            ]

        await asyncio.gather(
            *(operation.cancel() for operation in operations),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(operation.wait() for operation in operations),
            return_exceptions=True,
        )
        await self._registry.close()


    @staticmethod
    def _parse_operation_id(operation_id: UUID | str) -> UUID:
        try:
            return operation_id if isinstance(operation_id, UUID) else UUID(operation_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("operation_id must be a valid UUID") from exc


    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("operation manager is closed")
