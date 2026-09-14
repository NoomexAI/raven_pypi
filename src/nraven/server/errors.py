"""Uniform HTTP error translation for Raven's FastAPI boundary."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.responses import Response

from ..core.errors import ErrorCode, RavenError
from .schemas import ErrorBody, ErrorResponse


LOGGER = logging.getLogger("nraven.server")
REQUEST_ID_HEADER = "X-Request-ID"


_NOT_FOUND_CODES = {
    ErrorCode.KNOWLEDGE_NOT_FOUND,
    ErrorCode.CONVERSATION_NOT_FOUND,
    ErrorCode.CONVERSATION_TURN_NOT_FOUND,
    ErrorCode.FILE_NOT_FOUND,
    ErrorCode.SECTION_NOT_FOUND,
    ErrorCode.OPERATION_NOT_FOUND,
    ErrorCode.OPERATION_TASK_NOT_FOUND,
}

_CONFLICT_CODES = {
    ErrorCode.KNOWLEDGE_ALREADY_EXISTS,
    ErrorCode.CONVERSATION_ALREADY_EXISTS,
    ErrorCode.FILE_ALREADY_EXISTS,
    ErrorCode.CONVERSATION_TURN_CONFLICT,
    ErrorCode.CONVERSATION_SESSION_ACTIVE,
    ErrorCode.CONVERSATION_SESSION_CONFIGURATION_CONFLICT,
    ErrorCode.OPERATION_FINISHED,
    ErrorCode.OPERATION_TASK_NOT_RETRYABLE,
    ErrorCode.OPERATION_TASK_ALREADY_RETRIED,
    ErrorCode.RUNTIME_CONFIG_CONFLICT,
}

_UNAVAILABLE_CODES = {
    ErrorCode.OLLAMA_UNAVAILABLE,
    ErrorCode.MODEL_PROVIDER_FAILED,
    ErrorCode.OPERATION_MANAGER_CLOSED,
    ErrorCode.RUNTIME_REGISTRY_CLOSED,
    ErrorCode.RUNTIME_CAPACITY_EXCEEDED,
    ErrorCode.OPERATION_DATABASE_IN_USE,
    ErrorCode.OPERATION_SYNC_FAILED,
}


def install_error_handling(app: FastAPI) -> None:
    """Install request IDs and sanitized exception handlers on one app."""

    @app.middleware("http")
    async def assign_request_id(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        return response

    @app.exception_handler(RavenError)
    async def handle_raven_error(
        request: Request,
        error: RavenError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=_status_for_raven_error(error.code),
            code=error.code.value,
            message=error.message,
            details=error.details,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        issues = [
            {
                "location": [str(part) for part in issue.get("loc", ())],
                "message": str(issue.get("msg", "Invalid value.")),
                "type": str(issue.get("type", "validation_error")),
            }
            for issue in error.errors()
        ]
        return _error_response(
            request,
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="request_validation_error",
            message="The request is invalid.",
            details={"issues": issues},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request,
        error: Exception,
    ) -> JSONResponse:
        request_id = _request_id(request)
        LOGGER.exception(
            "Unexpected request failure request_id=%s",
            request_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        return _error_response(
            request,
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR.value,
            message="An unexpected internal error occurred.",
        )


def _status_for_raven_error(code: ErrorCode) -> int:
    if code in _NOT_FOUND_CODES:
        return HTTPStatus.NOT_FOUND
    if code in _CONFLICT_CODES:
        return HTTPStatus.CONFLICT
    if code in _UNAVAILABLE_CODES:
        return HTTPStatus.SERVICE_UNAVAILABLE
    if code == ErrorCode.EVENT_HISTORY_GAP:
        return HTTPStatus.GONE
    if code in {ErrorCode.OPERATION_CANCELLED, ErrorCode.OPERATION_INTERRUPTED}:
        return HTTPStatus.CONFLICT
    if code in {
        ErrorCode.INTERNAL_ERROR,
        ErrorCode.PERSISTENCE_FAILED,
        ErrorCode.OPERATION_DATABASE_FAILED,
        ErrorCode.OPERATION_DATABASE_CORRUPTED,
    }:
        return HTTPStatus.INTERNAL_SERVER_ERROR
    return HTTPStatus.BAD_REQUEST


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=details or {},
        ),
        request_id=_request_id(request),
    )
    return JSONResponse(
        status_code=int(status_code),
        content=payload.model_dump(mode="json"),
        headers={REQUEST_ID_HEADER: payload.request_id},
    )


def _request_id(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id
    request_id = str(uuid4())
    request.state.request_id = request_id
    return request_id
