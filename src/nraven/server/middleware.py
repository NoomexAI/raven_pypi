"""Low-level ASGI request limits applied before route body parsing."""

from __future__ import annotations

from uuid import uuid4

from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..core.errors import ErrorCode
from .errors import REQUEST_ID_HEADER
from .schemas import ErrorBody, ErrorResponse


class UploadBodyLimitMiddleware:
    """Bound multipart upload bytes before Starlette can spool them to disk."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        maximum_file_bytes: int,
        maximum_overhead_bytes: int,
    ) -> None:
        self.app = app
        self.maximum_request_bytes = maximum_file_bytes + maximum_overhead_bytes


    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if not _is_upload_request(scope):
            await self.app(scope, receive, send)
            return

        content_length = _content_length(scope)
        if (
            content_length is not None
            and content_length > self.maximum_request_bytes
        ):
            await self._reject(scope, receive, send)
            return

        received = 0
        overflowed = False
        response_messages: list[Message] = []

        async def limited_receive() -> Message:
            nonlocal overflowed, received
            if overflowed:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.maximum_request_bytes:
                    overflowed = True
                    return {"type": "http.disconnect"}
            return message

        async def buffered_send(message: Message) -> None:
            response_messages.append(message)

        try:
            await self.app(scope, limited_receive, buffered_send)
        except ClientDisconnect:
            if not overflowed:
                raise

        if overflowed:
            await self._reject(scope, receive, send)
            return
        for message in response_messages:
            await send(message)


    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        state = scope.get("state")
        request_id = (
            state.get("request_id")
            if isinstance(state, dict)
            else None
        )
        if not isinstance(request_id, str) or not request_id:
            request_id = str(uuid4())
        payload = ErrorResponse(
            error=ErrorBody(
                code=ErrorCode.SOURCE_FILE_TOO_LARGE.value,
                message="The upload request exceeds the configured size limit.",
                details={"max_request_bytes": self.maximum_request_bytes},
            ),
            request_id=request_id,
        )
        response = JSONResponse(
            status_code=413,
            content=payload.model_dump(mode="json"),
            headers={REQUEST_ID_HEADER: request_id},
        )
        await response(scope, receive, send)




def _is_upload_request(scope: Scope) -> bool:
    if scope["type"] != "http" or scope.get("method") != "POST":
        return False
    parts = scope.get("path", "").strip("/").split("/")
    if (
        len(parts) != 5
        or parts[:3] != ["api", "v1", "knowledges"]
        or parts[4] != "files"
    ):
        return False
    content_type = _header(scope, b"content-type")
    return (
        content_type is not None
        and content_type.lower().startswith(b"multipart/form-data")
    )


def _content_length(scope: Scope) -> int | None:
    value = _header(scope, b"content-length")
    if value is None or not value.isdigit():
        return None
    if len(value) > 20:
        return 1 << 64
    return int(value)


def _header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value
    return None
