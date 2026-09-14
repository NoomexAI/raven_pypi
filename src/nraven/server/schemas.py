"""Shared HTTP response contracts for the Raven server."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
