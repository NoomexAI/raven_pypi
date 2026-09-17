"""Low-level ASGI request limits applied before route body parsing."""

from __future__ import annotations

from uuid import uuid4

from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..core.errors import ErrorCode
from .errors import REQUEST_ID_HEADER
from .schemas import ErrorBody, ErrorResponse


class RequestLimitMiddleware:
    """Bound query strings and request bodies before framework parsing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        maximum_body_bytes: int,
        maximum_file_bytes: int,
        maximum_overhead_bytes: int,
        maximum_query_string_bytes: int,
    ) -> None:
        self.app = app
        self.maximum_body_bytes = maximum_body_bytes
        self.maximum_upload_bytes = maximum_file_bytes + maximum_overhead_bytes
        self.maximum_query_string_bytes = maximum_query_string_bytes


    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        query_string = scope.get("query_string", b"")
        if len(query_string) > self.maximum_query_string_bytes:
            await self._reject(
                scope,
                receive,
                send,
                status_code=414,
                code=ErrorCode.REQUEST_QUERY_TOO_LARGE,
                message="The request query string exceeds the configured size limit.",
                limit=self.maximum_query_string_bytes,
            )
            return

        if scope.get("method") not in {"POST", "PUT", "PATCH", "DELETE"}:
            await self.app(scope, receive, send)
            return

        maximum_request_bytes = (
            self.maximum_upload_bytes
            if _is_multipart_request(scope)
            else self.maximum_body_bytes
        )

        content_length = _content_length(scope)
        if (
            content_length is not None
            and content_length > maximum_request_bytes
        ):
            await self._reject_body(scope, receive, send, maximum_request_bytes)
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
                if received > maximum_request_bytes:
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
            await self._reject_body(scope, receive, send, maximum_request_bytes)
            return
        for message in response_messages:
            await send(message)


    async def _reject_body(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        maximum_request_bytes: int,
    ) -> None:
        upload = _is_multipart_request(scope)
        await self._reject(
            scope,
            receive,
            send,
            status_code=413,
            code=(
                ErrorCode.SOURCE_FILE_TOO_LARGE
                if upload
                else ErrorCode.REQUEST_BODY_TOO_LARGE
            ),
            message=(
                "The upload request exceeds the configured size limit."
                if upload
                else "The request body exceeds the configured size limit."
            ),
            limit=maximum_request_bytes,
        )


    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        code: ErrorCode,
        message: str,
        limit: int,
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
                code=code.value,
                message=message,
                details={"maximum_bytes": limit},
            ),
            request_id=request_id,
        )
        response = JSONResponse(
            status_code=status_code,
            content=payload.model_dump(mode="json"),
            headers={REQUEST_ID_HEADER: request_id},
        )
        await response(scope, receive, send)




def _is_multipart_request(scope: Scope) -> bool:
    content_type = _header(scope, b"content-type")
    if content_type is None:
        return False
    media_type = content_type.strip().split(b";", 1)[0].strip().lower()
    return media_type == b"multipart/form-data"


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
