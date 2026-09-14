"""FastAPI application factory for Raven."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from ..core.config import DEFAULT_USER_ID, SystemConfig, load_system_config
from .errors import install_error_handling
from .routers.health import router as health_router
from .runtime import UserRuntimeRegistry


def create_app(
    raven_home: str | Path,
    system_config: SystemConfig | None = None,
) -> FastAPI:
    """Create one independently owned Raven ASGI application."""
    resolved_home = Path(raven_home).expanduser().resolve()
    config = system_config or load_system_config(resolved_home)
    registry = UserRuntimeRegistry(resolved_home, config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        await registry.start()
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
    app.state.runtime_registry = registry
    install_error_handling(app)
    app.include_router(health_router)
    return app
