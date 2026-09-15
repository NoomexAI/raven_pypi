"""Operation lifecycle and replayable SSE endpoints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import StreamingResponse

from ...core.errors import ErrorCode, RavenError
from ...core.events import Event
from ...core.operations import OperationStatus, OperationType
from ...h_api.raven import Raven
from ..dependencies import get_runtime_registry, lease_raven, resolve_user_id
from ..schemas import (
    ErrorResponse,
    OperationPageResponse,
    OperationResponse,
    OperationTaskPageResponse,
    OperationTaskReference,
    OperationTaskResponse,
    SSEEventEnvelope,
)


router = APIRouter(tags=["operations"])
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    410: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


@router.get(
    "/api/v1/operations",
    response_model=OperationPageResponse,
    responses=_ERROR_RESPONSES,
)
async def list_operations(
    raven: Annotated[Raven, Depends(lease_raven)],
    operation_status: Annotated[
        OperationStatus | None,
        Query(alias="status"),
    ] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
    after_operation_id: UUID | None = None,
) -> OperationPageResponse:
    page_size = _page_size(raven, limit)
    records = await raven.list_operations(
        status=operation_status,
        limit=page_size + 1,
        after_operation_id=after_operation_id,
    )
    has_more = len(records) > page_size
    records = records[:page_size]
    return OperationPageResponse(
        items=[OperationResponse.from_record(record) for record in records],
        next_cursor=(
            records[-1].operation_id if has_more and records else None
        ),
    )


@router.get(
    "/api/v1/operation-tasks/retryable",
    response_model=OperationTaskPageResponse,
    responses=_ERROR_RESPONSES,
)
async def list_retryable_tasks(
    raven: Annotated[Raven, Depends(lease_raven)],
    limit: Annotated[int | None, Query(ge=1)] = None,
    after_task_id: UUID | None = None,
) -> OperationTaskPageResponse:
    page_size = _page_size(raven, limit)
    records = await raven.list_retryable_tasks(
        limit=page_size + 1,
        after_task_id=after_task_id,
    )
    has_more = len(records) > page_size
    records = records[:page_size]
    return OperationTaskPageResponse(
        items=[OperationTaskResponse.from_record(record) for record in records],
        next_cursor=records[-1].task_id if has_more and records else None,
    )


@router.get(
    "/api/v1/operations/{operation_id}",
    response_model=OperationResponse,
    responses=_ERROR_RESPONSES,
)
async def get_operation(
    operation_id: UUID,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> OperationResponse:
    record = await raven.get_operation_record(operation_id)
    return OperationResponse.from_record(record)


@router.get(
    "/api/v1/operations/{operation_id}/tasks",
    response_model=OperationTaskPageResponse,
    responses=_ERROR_RESPONSES,
)
async def list_operation_tasks(
    operation_id: UUID,
    raven: Annotated[Raven, Depends(lease_raven)],
    task_status: Annotated[
        OperationStatus | None,
        Query(alias="status"),
    ] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
    after_task_id: UUID | None = None,
) -> OperationTaskPageResponse:
    page_size = _page_size(raven, limit)
    records = await raven.list_operation_tasks(
        operation_id,
        status=task_status,
        limit=page_size + 1,
        after_task_id=after_task_id,
    )
    has_more = len(records) > page_size
    records = records[:page_size]
    return OperationTaskPageResponse(
        items=[OperationTaskResponse.from_record(record) for record in records],
        next_cursor=records[-1].task_id if has_more and records else None,
    )


@router.get(
    "/api/v1/operations/{operation_id}/tasks/{task_id}",
    response_model=OperationTaskResponse,
    responses=_ERROR_RESPONSES,
)
async def get_operation_task(
    operation_id: UUID,
    task_id: UUID,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> OperationTaskResponse:
    record = await raven.get_operation_task(operation_id, task_id)
    return OperationTaskResponse.from_record(record)


@router.get(
    "/api/v1/operations/{operation_id}/events",
    response_model=SSEEventEnvelope,
    response_class=StreamingResponse,
    responses={
        **_ERROR_RESPONSES,
        200: {
            "description": "Replayable operation event stream.",
            "content": {
                "text/event-stream": {
                    "schema": {
                        "$ref": "#/components/schemas/SSEEventEnvelope",
                    }
                }
            },
        },
    },
)
async def operation_events(
    operation_id: UUID,
    raven: Annotated[Raven, Depends(lease_raven)],
    last_event_id: Annotated[
        str | None,
        Header(alias="Last-Event-ID"),
    ] = None,
) -> StreamingResponse:
    cursor = _event_cursor(last_event_id)
    operation = await raven.get_operation_record(operation_id)
    available = await raven.read_operation_events(
        operation_id,
        after_event_id=cursor,
        limit=1,
    )
    if available:
        first = available[0]
        assert first.event_id is not None
        events = _prepend_event(
            first,
            raven.operation_events(
                operation_id,
                after_event_id=first.event_id,
            ),
        )
    elif operation.is_finished:
        events = _empty_events()
    else:
        events = raven.operation_events(
            operation_id,
            after_event_id=cursor,
        )
    return StreamingResponse(
        _sse_frames(
            events,
            heartbeat_interval=raven.system_config.sse_heartbeat_interval_seconds,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/api/v1/operations/{operation_id}/cancel",
    response_model=OperationResponse,
    responses=_ERROR_RESPONSES,
)
async def cancel_operation(
    operation_id: UUID,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> OperationResponse:
    await raven.cancel_operation(operation_id)
    record = await raven.get_operation_record(operation_id)
    return OperationResponse.from_record(record)


@router.post(
    "/api/v1/operations/{operation_id}/tasks/{task_id}/retry",
    response_model=OperationTaskReference,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def retry_operation_task(
    operation_id: UUID,
    task_id: UUID,
    raven: Annotated[Raven, Depends(lease_raven)],
    request: Request,
) -> OperationTaskReference:
    failed = await raven.get_operation_task(operation_id, task_id)
    task = await raven.retry_task(operation_id, task_id)
    if failed.name == OperationType.INGESTION_RUN.value:
        source_value = (failed.retry_input or {}).get("source_path")
        if isinstance(source_value, str):
            source = Path(source_value).resolve()
            root = (raven.paths.uploads_dir / "browser").resolve()
            if source.is_relative_to(root):
                get_runtime_registry(request).track_upload(
                    resolve_user_id(request),
                    task,
                    source,
                    raven.paths.uploads_dir,
                )
    return OperationTaskReference.from_task(task)


def _page_size(raven: Raven, requested: int | None) -> int:
    maximum = raven.system_config.operation_page_size
    if requested is None:
        return maximum
    if requested > maximum:
        raise RavenError(
            ErrorCode.INVALID_OPERATION_PAGE_SIZE,
            f"Operation page size cannot exceed {maximum}.",
            details={"maximum": maximum},
        )
    return requested


def _event_cursor(value: str | None) -> int:
    if value is None:
        return 0
    if not value.isascii() or not value.isdecimal():
        raise RavenError(
            ErrorCode.INVALID_EVENT_CURSOR,
            "Last-Event-ID must be a non-negative integer.",
        )
    return int(value)


async def _prepend_event(
    first: Event,
    remaining: AsyncIterator[Event],
) -> AsyncGenerator[Event, None]:
    yield first
    if first.is_final:
        return
    async for event in remaining:
        yield event


async def _empty_events() -> AsyncGenerator[Event, None]:
    empty: tuple[Event, ...] = ()
    for event in empty:
        yield event


async def _next_event(iterator: AsyncIterator[Event]) -> Event:
    """Give asyncio a coroutine instead of an arbitrary awaitable."""
    return await anext(iterator)


async def _sse_frames(
    events: AsyncIterator[Event],
    *,
    heartbeat_interval: float,
) -> AsyncGenerator[str, None]:
    iterator = events.__aiter__()
    pending: asyncio.Task[Event] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(_next_event(iterator))
            done, _pending = await asyncio.wait(
                {pending},
                timeout=heartbeat_interval,
            )
            if not done:
                yield ": heartbeat\n\n"
                continue

            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None

            yield _serialize_sse_event(event)
            if event.is_final:
                return
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if isinstance(iterator, AsyncGenerator):
            await iterator.aclose()


def _serialize_sse_event(event: Event) -> str:
    envelope = SSEEventEnvelope.from_event(event)
    payload = json.dumps(
        envelope.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"id: {envelope.event_id}\n"
        f"event: {envelope.type.value}\n"
        f"data: {payload}\n\n"
    )
