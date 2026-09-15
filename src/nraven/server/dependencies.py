"""Request-scoped dependencies for user-isolated Raven runtimes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request

from ..core.errors import ErrorCode, RavenError
from ..h_api.raven import Raven
from .runtime import UserRuntimeRegistry


def get_runtime_registry(request: Request) -> UserRuntimeRegistry:
    registry = getattr(request.app.state, "runtime_registry", None)
    if not isinstance(registry, UserRuntimeRegistry):
        raise RavenError(
            ErrorCode.RUNTIME_REGISTRY_CLOSED,
            "The user runtime registry is unavailable.",
        )
    return registry


def resolve_user_id(request: Request) -> UUID:
    """Use the identity verified for this request by the desktop or host boundary."""
    value = getattr(request.state, "user_id", None)
    if value is None:
        raise RavenError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            "The request has no verified user identity.",
        )
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise RavenError(
            ErrorCode.INVALID_USER_ID,
            "The request did not resolve to a valid user UUID.",
        ) from exc


async def lease_raven(
    user_id: Annotated[UUID, Depends(resolve_user_id)],
    registry: Annotated[UserRuntimeRegistry, Depends(get_runtime_registry)],
) -> AsyncIterator[Raven]:
    """Lease the user's Raven runtime for the complete request lifetime."""
    async with registry.lease(user_id) as raven:
        yield raven
