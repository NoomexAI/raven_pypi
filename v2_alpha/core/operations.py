"""Operation lifecycle and task execution for Raven."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from .errors import ErrorCode, RavenError, error_payload
from .events import Event, EventStream, EventStreamRegistry, EventType



class OperationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"



OperationWorker = Callable[["Operation"], Awaitable[Any]]
TaskWorker = Callable[["Operation"], Awaitable[Any]]
_TERMINAL_STATUSES = frozenset(
    {
        OperationStatus.COMPLETED,
        OperationStatus.FAILED,
        OperationStatus.CANCELLED,
    }
)
_CURRENT_TASK: ContextVar[tuple[UUID, str] | None] = ContextVar(
    "raven_current_operation_task",
    default=None,
)



class Operation:
    """One root operation, its event stream, and its child task registry."""

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
        self._error: dict[str, Any] | None = None
        self._cancellation_requested = False
        self._task: asyncio.Task[Any] | None = None
        self._child_tasks: dict[UUID, OperationTask] = {}
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
    def error(self) -> dict[str, Any] | None:
        return self._error


    @property
    def cancellation_requested(self) -> bool:
        return self._cancellation_requested


    @property
    def is_finished(self) -> bool:
        return self._status in _TERMINAL_STATUSES


    @property
    def tasks(self) -> tuple["OperationTask", ...]:
        return tuple(self._child_tasks.values())


    async def start(self, worker: OperationWorker) -> None:
        """Queue this root operation and start its worker task."""
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
        """Wait for the root worker and return this operation."""
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return self


    async def cancel(self) -> "Operation":
        """Cancel the root worker and all active child tasks."""
        async with self._lock:
            if self.is_finished:
                return self
            self._cancellation_requested = True
            root_task = self._task
            child_tasks = tuple(
                task for task in self._child_tasks.values() if not task._root
            )

        for child_task in child_tasks:
            await child_task.cancel()
        await asyncio.gather(
            *(
                child_task._task
                for child_task in child_tasks
                if child_task._task is not None
            ),
            return_exceptions=True,
        )
        if root_task is not None and not root_task.done():
            root_task.cancel()
            await asyncio.gather(root_task, return_exceptions=True)
        elif root_task is None:
            await self._finish_cancelled()
        return self


    async def publish(self, event: Event) -> Event:
        """Publish a domain event and attach the active task correlation."""
        if self.is_finished:
            raise RuntimeError(f"operation '{self.operation_id}' is already finished")
        if event.is_final:
            raise ValueError("workers cannot publish final operation events")
        if event.operation_id is not None and event.operation_id != self.operation_id:
            raise ValueError("event operation_id does not match the operation")

        current_task = _CURRENT_TASK.get()
        updates: dict[str, Any] = {"operation_id": self.operation_id}
        if current_task is not None:
            updates["task_id"] = event.task_id or current_task[0]
            updates["task_name"] = event.task_name or current_task[1]

        return await self._stream.publish(event.model_copy(update=updates))


    def raise_if_cancelled(self) -> None:
        if self._cancellation_requested:
            raise asyncio.CancelledError


    async def events(self, after_event_id: int = 0) -> AsyncIterator[Event]:
        async for event in self._stream.events(after_event_id=after_event_id):
            yield event


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
            error=error_payload(error),
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
        error: dict[str, Any] | None = None,
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


    @classmethod
    def from_events(
        cls,
        operation_id: UUID,
        stream: EventStream,
        events: list[Event],
    ) -> "Operation":
        """Reconstruct a root operation from its persisted event history."""
        if not events:
            raise RavenError(
                ErrorCode.OPERATION_NOT_FOUND,
                f"Operation '{operation_id}' has no persisted events.",
            )

        queued = next(
            (event for event in events if event.type == EventType.OPERATION_QUEUED),
            None,
        )
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


    def _register_task(self, task: "OperationTask") -> None:
        self._child_tasks[task.task_id] = task


    def _restore(self, events: list[Event]) -> None:
        started = next(
            (event for event in events if event.type == EventType.OPERATION_STARTED),
            None,
        )
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
            error = final.data.get("error")
            self._error = error if isinstance(error, dict) else None
        elif final.type == EventType.OPERATION_CANCELLED:
            self._status = OperationStatus.CANCELLED



class OperationTask:
    """Handle and result container for one invocation inside an operation."""

    def __init__(
        self,
        operation: Operation,
        name: str,
        worker: TaskWorker,
        *,
        root: bool,
        task_id: UUID | None = None,
    ) -> None:
        self.operation = operation
        self.task_id = task_id or uuid4()
        self.name = name
        self._worker = worker
        self._root = root
        self._status = OperationStatus.QUEUED
        self._result: Any = None
        self._error: BaseException | None = None
        self._task: asyncio.Task[Any] | None = None
        operation._register_task(self)


    @property
    def operation_id(self) -> UUID:
        return self.operation.operation_id


    @property
    def status(self) -> OperationStatus:
        return self._status


    @property
    def result_value(self) -> Any:
        """Return a completed result without waiting; prefer ``result()``."""
        return self._result


    @property
    def error(self) -> BaseException | None:
        return self._error


    @property
    def is_finished(self) -> bool:
        return self._status in _TERMINAL_STATUSES


    @property
    def is_root(self) -> bool:
        return self._root


    async def start(self) -> None:
        """Start a child task. Root tasks are started by Operation.start."""
        if self._root:
            raise RuntimeError("root operation tasks are started by Operation.start")
        if self._task is not None:
            raise RuntimeError(f"task '{self.task_id}' has already started")
        await self._publish_lifecycle(EventType.OPERATION_TASK_QUEUED)
        self._task = asyncio.create_task(
            self._run(),
            name=f"raven-operation-task-{self.task_id}",
        )


    async def _run_root(self, operation: Operation) -> Any:
        await self._publish_lifecycle(EventType.OPERATION_TASK_QUEUED)
        return await self._run()


    async def _run(self) -> Any:
        self._status = OperationStatus.RUNNING
        await self._publish_lifecycle(EventType.OPERATION_TASK_STARTED)
        token: Token[tuple[UUID, str] | None] = _CURRENT_TASK.set(
            (self.task_id, self.name)
        )
        try:
            self.operation.raise_if_cancelled()
            self._result = await self._worker(self.operation)
            self._status = OperationStatus.COMPLETED
            await self._publish_lifecycle(EventType.OPERATION_TASK_COMPLETED)
            return self._result
        except asyncio.CancelledError as exc:
            self._status = OperationStatus.CANCELLED
            self._error = exc
            await self._publish_lifecycle(EventType.OPERATION_TASK_CANCELLED)
            if self._root:
                raise
            return None
        except Exception as exc:
            self._status = OperationStatus.FAILED
            self._error = exc
            await self._publish_lifecycle(
                EventType.OPERATION_TASK_FAILED,
                {"error": error_payload(exc)},
            )
            if self._root:
                raise
            return None
        finally:
            _CURRENT_TASK.reset(token)


    async def result(self) -> Any:
        """Wait for this invocation and return or raise its native result."""
        if self._root:
            await self.operation.wait()
        elif self._task is not None:
            await self._task
        if self._status == OperationStatus.CANCELLED:
            raise RavenError(
                ErrorCode.OPERATION_CANCELLED,
                f"Operation task '{self.task_id}' was cancelled.",
            )
        if self._error is not None:
            raise self._error
        return self._result


    async def cancel(self) -> None:
        """Cancel this task, or its root operation when it is the root task."""
        if self._root:
            await self.operation.cancel()
            return
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        elif not self.is_finished:
            self._status = OperationStatus.CANCELLED


    async def events(self, after_event_id: int = 0) -> AsyncIterator[Event]:
        """Yield events correlated with this task from the parent stream."""
        async for event in self.operation.events(after_event_id=after_event_id):
            if event.task_id == self.task_id:
                yield event


    async def _publish_lifecycle(
        self,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> None:
        await self.operation.publish(
            Event(
                type=event_type,
                data={"name": self.name, **(data or {})},
                task_id=self.task_id,
                task_name=self.name,
            )
        )



class OperationManager:
    """Create root operations and execute correlated child tasks."""

    def __init__(self, registry: EventStreamRegistry) -> None:
        self._registry = registry
        self._operations: dict[str, Operation] = {}
        self._lock = asyncio.Lock()
        self._closed = False


    async def submit(self, name: str, worker: OperationWorker) -> Operation:
        """Create and start a root operation using a low-level worker."""
        operation = await self._create_operation(name)
        await operation.start(worker)
        return operation


    async def run(
        self,
        name: str,
        worker: TaskWorker,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Run one component invocation in a new or supplied operation."""
        if operation is None:
            root = await self._create_operation(name)
            task = OperationTask(root, name, worker, root=True)
            await root.start(task._run_root)
            return task

        self._ensure_operation_active(operation)
        task = OperationTask(operation, name, worker, root=False)
        await task.start()
        return task


    async def execute(
        self,
        name: str,
        worker: TaskWorker,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Explicit alias for ``run`` when used by component wrappers."""
        return await self.run(name, worker, operation=operation)


    async def get(self, operation_id: UUID | str) -> Operation:
        parsed_id = self._parse_operation_id(operation_id)
        key = str(parsed_id)
        async with self._lock:
            operation = self._operations.get(key)
            if operation is not None:
                return operation

        if key not in self._registry.stored_operation_ids():
            raise RavenError(
                ErrorCode.OPERATION_NOT_FOUND,
                f"Operation '{key}' was not found.",
            )
        stream = self._registry.get(key)
        operation = Operation.from_events(
            operation_id=parsed_id,
            stream=stream,
            events=await stream.read(),
        )
        async with self._lock:
            return self._operations.setdefault(key, operation)


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


    async def _create_operation(self, name: str) -> Operation:
        if not isinstance(name, str) or not name.strip():
            raise RavenError(
                ErrorCode.INVALID_OPERATION_NAME,
                "Operation name cannot be empty.",
            )
        operation_id = uuid4()
        key = str(operation_id)
        async with self._lock:
            self._ensure_open()
            operation = Operation(
                operation_id=operation_id,
                name=name,
                stream=self._registry.get(key),
            )
            self._operations[key] = operation
            return operation


    @staticmethod
    def _parse_operation_id(operation_id: UUID | str) -> UUID:
        try:
            return operation_id if isinstance(operation_id, UUID) else UUID(operation_id)
        except (TypeError, ValueError) as exc:
            raise RavenError(
                ErrorCode.INVALID_OPERATION_ID,
                "operation_id must be a valid UUID.",
            ) from exc


    @staticmethod
    def _ensure_operation_active(operation: Operation) -> None:
        if operation.is_finished:
            raise RavenError(
                ErrorCode.OPERATION_FINISHED,
                f"Operation '{operation.operation_id}' is already finished.",
            )


    def _ensure_open(self) -> None:
        if self._closed:
            raise RavenError(
                ErrorCode.OPERATION_MANAGER_CLOSED,
                "Operation manager is closed.",
            )
