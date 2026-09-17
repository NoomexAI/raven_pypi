"""FastAPI application factory for Raven."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import token_urlsafe

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..core.config import DEFAULT_USER_ID, SystemConfig, load_system_config
from .errors import install_error_handling
from .middleware import RequestLimitMiddleware
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
) -> FastAPI:
    """Create Raven's authenticated ASGI application."""
    resolved_home = Path(raven_home).expanduser().resolve()
    config = system_config or load_system_config(resolved_home)
    registry = UserRuntimeRegistry(resolved_home, config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        await registry.start()
        try:
            await registry.cleanup_uploads()
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
    app.state.bearer_token = token_urlsafe(32)
    app.add_middleware(
        RequestLimitMiddleware,
        maximum_body_bytes=config.max_request_body_bytes,
        maximum_file_bytes=config.max_source_file_bytes,
        maximum_overhead_bytes=config.max_upload_request_overhead_bytes,
        maximum_query_string_bytes=config.max_query_string_bytes,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Last-Event-ID",
            "X-Raven-User-ID",
        ],
        expose_headers=["X-Request-ID"],
    )
    install_error_handling(app)
    app.include_router(health_router)
    app.include_router(operations_router)
    app.include_router(runtime_router)
    app.include_router(models_router)
    app.include_router(knowledges_router)
    app.include_router(conversations_router)
    return app
