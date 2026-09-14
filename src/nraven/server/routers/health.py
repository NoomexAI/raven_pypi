"""Foundational liveness and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...core.errors import ErrorCode, RavenError
from ..schemas import ErrorResponse, HealthResponse


router = APIRouter(prefix="/api/v1/health", tags=["health"])


@router.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/ready",
    response_model=HealthResponse,
    responses={503: {"model": ErrorResponse}},
)
async def ready(request: Request) -> HealthResponse:
    registry = getattr(request.app.state, "runtime_registry", None)
    if registry is None or not bool(getattr(registry, "is_ready", False)):
        raise RavenError(
            ErrorCode.RUNTIME_REGISTRY_CLOSED,
            "The Raven runtime registry is not ready.",
        )
    return HealthResponse(status="ready")
