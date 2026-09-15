"""FastAPI application factory for Raven."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
from secrets import token_urlsafe
from uuid import UUID

from fastapi import FastAPI, Request

from ..core.config import DEFAULT_USER_ID, SystemConfig, load_system_config
from .errors import install_error_handling
from .routers.conversations import router as conversations_router
from .routers.health import router as health_router
from .routers.knowledges import router as knowledges_router
from .routers.models import router as models_router
from .routers.operations import router as operations_router
from .routers.runtime import router as runtime_router
from .runtime import UserRuntimeRegistry


def create_app(
    raven_home: str | Path,
    system_config: SystemConfig | None = None,
    *,
    identity_resolver: Callable[[Request], UUID | Awaitable[UUID] | None] | None = None,
) -> FastAPI:
    """Create a desktop app, or a hosted app with a trusted identity resolver."""
    resolved_home = Path(raven_home).expanduser().resolve()
    config = system_config or load_system_config(resolved_home)
    try:
        loopback = ip_address(config.host).is_loopback
    except ValueError:
        loopback = config.host == "localhost"
    if not loopback and identity_resolver is None:
        raise ValueError(
            "The built-in desktop authentication requires a loopback host. "
            "Hosted authentication is not configured."
        )
    registry = UserRuntimeRegistry(resolved_home, config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        await registry.start()
        await registry.cleanup_uploads()
        try:
            yield
        finally:
            await registry.close()

    app = FastAPI(
        title="RAVEN API",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.raven_home = resolved_home
    app.state.system_config = config
    app.state.default_user_id = DEFAULT_USER_ID
    app.state.identity_resolver = identity_resolver
    app.state.runtime_registry = registry
    app.state.bearer_token = token_urlsafe(32)
    install_error_handling(app)
    app.include_router(health_router)
    app.include_router(operations_router)
    app.include_router(runtime_router)
    app.include_router(models_router)
    app.include_router(knowledges_router)
    app.include_router(conversations_router)
    return app
