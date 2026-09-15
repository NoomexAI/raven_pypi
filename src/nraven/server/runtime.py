"""User-scoped Raven runtime ownership for the ASGI application."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from ..core.async_utils import await_completion
from ..core.config import SystemConfig
from ..core.errors import ErrorCode, RavenError
from ..core.operations import OperationTask
from ..core.upload_retention import cleanup_expired_uploads, remove_upload_source
from ..h_api.raven import Raven


RavenFactory = Callable[..., Raven]


@dataclass(slots=True)
class _RuntimeEntry:
    raven: Raven
    leases: int
    idle_since: float




class UserRuntimeRegistry:
    """Lazily own one Raven runtime for each validated user UUID."""

    def __init__(
        self,
        raven_home: str | Path,
        system_config: SystemConfig,
        *,
        raven_factory: RavenFactory = Raven,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.raven_home = Path(raven_home).expanduser().resolve()
        self.system_config = system_config
        self._raven_factory = raven_factory
        self._clock = clock
        self._entries: dict[UUID, _RuntimeEntry] = {}
        self._initializing: dict[UUID, asyncio.Task[Raven]] = {}
        self._lock = asyncio.Lock()
        self._maintenance_task: asyncio.Task[None] | None = None
        self._upload_monitors: dict[asyncio.Task[None], Path] = {}
        self._started = False
        self._closed = False


    @property
    def is_ready(self) -> bool:
        return self._started and not self._closed


    @property
    def runtime_count(self) -> int:
        return len(self._entries)


    async def start(self) -> None:
        """Start registry maintenance without eagerly creating user runtimes."""
        async with self._lock:
            if self._closed:
                raise RavenError(
                    ErrorCode.RUNTIME_REGISTRY_CLOSED,
                    "The user runtime registry is closed.",
                )
            if self._started:
                return
            self._started = True
            self._maintenance_task = asyncio.create_task(
                self._maintain(),
                name="nraven-user-runtime-maintenance",
            )


    @asynccontextmanager
    async def lease(self, user_id: UUID) -> AsyncGenerator[Raven, None]:
        """Keep one user's runtime alive for the duration of a request."""
        if not isinstance(user_id, UUID):
            raise RavenError(
                ErrorCode.INVALID_USER_ID,
                "user_id must be a valid UUID.",
            )
        raven = await self._acquire(user_id)
        try:
            yield raven
        finally:
            await self._release(user_id, raven)


    async def evict_idle(self, *, now: float | None = None) -> int:
        """Close runtimes that have remained unleased beyond the idle limit."""
        current = self._clock() if now is None else now
        cutoff = current - self.system_config.runtime_idle_seconds
        async with self._lock:
            if self._closed:
                return 0
            selected = [
                (user_id, entry.raven)
                for user_id, entry in self._entries.items()
                if entry.leases == 0 and entry.idle_since <= cutoff
            ]
            for user_id, _raven in selected:
                self._entries.pop(user_id, None)

        await self._close_runtimes([raven for _user_id, raven in selected])
        return len(selected)


    def track_upload(
        self,
        user_id: UUID,
        task: OperationTask,
        source: Path,
        uploads_dir: Path,
    ) -> None:
        """Own outcome cleanup independently of the submitting HTTP request."""
        monitor = asyncio.create_task(
            self._observe_upload(user_id, task, source, uploads_dir),
            name=f"nraven-upload-{task.task_id}",
        )
        self._upload_monitors[monitor] = source.resolve()
        monitor.add_done_callback(lambda finished: self._upload_monitors.pop(finished, None))


    async def _observe_upload(
        self,
        user_id: UUID,
        task: OperationTask,
        source: Path,
        uploads_dir: Path,
    ) -> None:
        try:
            async with self.lease(user_id):
                try:
                    await task.result()
                except (Exception, asyncio.CancelledError):
                    return
                await asyncio.to_thread(remove_upload_source, source, uploads_dir)
        except (Exception, asyncio.CancelledError):
            # Preserve the source for a bounded retry if monitoring is interrupted.
            return


    async def close(self) -> None:
        """Stop maintenance and close every runtime owned or being initialized."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._started = False
            maintenance = self._maintenance_task
            self._maintenance_task = None
            initialization_tasks = tuple(self._initializing.values())
            self._initializing.clear()
            runtimes = [entry.raven for entry in self._entries.values()]
            self._entries.clear()
            monitors = tuple(self._upload_monitors)
            self._upload_monitors.clear()

        if maintenance is not None:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)

        for monitor in monitors:
            monitor.cancel()
        await asyncio.gather(*monitors, return_exceptions=True)

        initialized = await asyncio.gather(
            *initialization_tasks,
            return_exceptions=True,
        )
        runtimes.extend(
            value
            for value in initialized
            if not isinstance(value, BaseException)
        )
        await self._close_runtimes(runtimes)


    async def _acquire(self, user_id: UUID) -> Raven:
        await self.start()
        evicted: Raven | None = None
        async with self._lock:
            self._ensure_open()
            existing = self._entries.get(user_id)
            if existing is not None:
                existing.leases += 1
                return existing.raven

            initialization = self._initializing.get(user_id)
            if initialization is None:
                evicted = self._reserve_capacity()
                initialization = asyncio.create_task(
                    self._create_runtime(user_id),
                    name=f"nraven-user-runtime-start-{user_id}",
                )
                self._initializing[user_id] = initialization

        if evicted is not None:
            await evicted.close()

        try:
            raven, cancellation_requested = await await_completion(initialization)
        except BaseException:
            async with self._lock:
                if self._initializing.get(user_id) is initialization:
                    self._initializing.pop(user_id, None)
            raise

        close_after_acquire = False
        async with self._lock:
            if self._initializing.get(user_id) is initialization:
                self._initializing.pop(user_id, None)
            if self._closed:
                close_after_acquire = True
            else:
                entry = self._entries.get(user_id)
                if entry is None:
                    entry = _RuntimeEntry(
                        raven=raven,
                        leases=0,
                        idle_since=self._clock(),
                    )
                    self._entries[user_id] = entry
                if not cancellation_requested:
                    entry.leases += 1
                raven = entry.raven

        if close_after_acquire:
            await raven.close()
            raise RavenError(
                ErrorCode.RUNTIME_REGISTRY_CLOSED,
                "The user runtime registry closed while the runtime was starting.",
            )
        if cancellation_requested:
            raise asyncio.CancelledError
        return raven


    async def _release(self, user_id: UUID, raven: Raven) -> None:
        async with self._lock:
            entry = self._entries.get(user_id)
            if entry is None or entry.raven is not raven:
                return
            if entry.leases > 0:
                entry.leases -= 1
            if entry.leases == 0:
                entry.idle_since = self._clock()


    def _reserve_capacity(self) -> Raven | None:
        occupied = len(self._entries) + len(self._initializing)
        if occupied < self.system_config.max_user_runtimes:
            return None
        idle = [
            (entry.idle_since, user_id, entry.raven)
            for user_id, entry in self._entries.items()
            if entry.leases == 0
        ]
        if not idle:
            raise RavenError(
                ErrorCode.RUNTIME_CAPACITY_EXCEEDED,
                "All configured user runtime slots are currently in use.",
                details={"limit": self.system_config.max_user_runtimes},
            )
        _idle_since, user_id, raven = min(idle, key=lambda item: item[0])
        self._entries.pop(user_id, None)
        return raven


    async def _create_runtime(self, user_id: UUID) -> Raven:
        raven = self._raven_factory(
            self.raven_home,
            user_id=user_id,
            system_config=self.system_config,
        )
        try:
            await raven.start()
        except BaseException:
            await asyncio.gather(raven.close(), return_exceptions=True)
            raise
        return raven


    async def _maintain(self) -> None:
        interval = min(
            60.0,
            max(1.0, self.system_config.runtime_idle_seconds / 2),
            self.system_config.operation_cleanup_interval_seconds,
        )
        try:
            while True:
                await asyncio.sleep(interval)
                await self.evict_idle()
                await self.cleanup_uploads()
        except asyncio.CancelledError:
            return


    async def cleanup_uploads(self) -> int:
        """Sweep expired private sources across users without opening runtimes."""
        active = frozenset(self._upload_monitors.values())
        return await asyncio.to_thread(self._cleanup_uploads_sync, active)


    def _cleanup_uploads_sync(self, active: frozenset[Path]) -> int:
        total = 0
        if not self.raven_home.is_dir():
            return 0
        for directory in self.raven_home.iterdir():
            if not directory.is_dir():
                continue
            try:
                UUID(directory.name)
            except ValueError:
                continue
            total += cleanup_expired_uploads(directory / "temp" / "uploads", active)
        return total


    async def _close_runtimes(self, runtimes: list[Raven]) -> None:
        unique: dict[int, Raven] = {id(raven): raven for raven in runtimes}
        results = await asyncio.gather(
            *(raven.close() for raven in unique.values()),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise RavenError(
                ErrorCode.INTERNAL_ERROR,
                "One or more user runtimes could not be closed.",
                details={"failure_count": len(failures)},
            ) from failures[0]


    def _ensure_open(self) -> None:
        if self._closed:
            raise RavenError(
                ErrorCode.RUNTIME_REGISTRY_CLOSED,
                "The user runtime registry is closed.",
            )
