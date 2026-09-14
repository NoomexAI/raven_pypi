"""Shared HTTP response contracts for the Raven server."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ..core.events import Event, EventType
from ..core.operations import (
    OperationRecord,
    OperationStatus,
    OperationTaskRecord,
    RetryPolicy,
)


class HealthResponse(BaseModel):
    """Minimal process health response used by foundational probes."""

    model_config = ConfigDict(extra="forbid")

    status: str




class ErrorBody(BaseModel):
    """Machine-readable error information safe for API consumers."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)




class ErrorResponse(BaseModel):
    """Uniform envelope returned by every HTTP error handler."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody
    request_id: str




class OperationResponse(BaseModel):
    """Public status for one durable operation."""

    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    name: str
    status: OperationStatus
    last_event_id: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None
    is_finished: bool


    @classmethod
    def from_record(cls, record: OperationRecord) -> "OperationResponse":
        return cls(
            **record.model_dump(),
            is_finished=record.is_finished,
        )




class OperationPageResponse(BaseModel):
    """One bounded page of operations in newest-first order."""

    model_config = ConfigDict(extra="forbid")

    items: list[OperationResponse]
    next_cursor: UUID | None = None




class OperationTaskResponse(BaseModel):
    """Public status for one task belonging to an operation."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    operation_id: UUID
    name: str
    is_root: bool
    status: OperationStatus
    retry_policy: RetryPolicy
    attempt: int
    max_attempts: int
    attempts_remaining: int
    can_retry: bool
    retry_of_operation_id: UUID | None
    retry_of_task_id: UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None


    @classmethod
    def from_record(cls, record: OperationTaskRecord) -> "OperationTaskResponse":
        values = record.model_dump(exclude={"retry_input"})
        return cls(
            **values,
            max_attempts=record.max_attempts,
            attempts_remaining=record.attempts_remaining,
            can_retry=record.can_retry,
        )




class OperationTaskPageResponse(BaseModel):
    """One bounded page of operation tasks in newest-first order."""

    model_config = ConfigDict(extra="forbid")

    items: list[OperationTaskResponse]
    next_cursor: UUID | None = None




class OperationTaskReference(BaseModel):
    """Reference returned when a new retry task has been accepted."""

    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    task_id: UUID
    status: OperationStatus
    events_url: str
    retry_of_operation_id: UUID | None = None
    retry_of_task_id: UUID | None = None




class SSEEventEnvelope(BaseModel):
    """JSON payload carried by one typed SSE event frame."""

    model_config = ConfigDict(extra="forbid")

    event_id: int
    operation_id: UUID
    task_id: UUID | None
    task_name: str | None
    type: EventType
    timestamp: datetime
    data: dict[str, Any]
    is_final: bool


    @classmethod
    def from_event(cls, event: Event) -> "SSEEventEnvelope":
        if event.event_id is None or event.operation_id is None:
            raise ValueError("Only persisted operation events can be serialized.")
        return cls(
            event_id=event.event_id,
            operation_id=event.operation_id,
            task_id=event.task_id,
            task_name=event.task_name,
            type=event.type,
            timestamp=event.timestamp,
            data=event.data,
            is_final=event.is_final,
        )
