from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

from ..events import Event, EventBus, EventType
from ..high_level_api import Raven
from ..operations import OperationManager, OperationStatus
from .schemas import (
    ChatTurnRequest,
    ConversationCreateRequest,
    ConversationPatchRequest,
    ConversationResponse,
    EventEnvelope,
    FileResponse,
    KnowledgeCreateRequest,
    KnowledgeSummaryResponse,
    ModelInspectResponse,
    ModelSummaryResponse,
    OperationReference,
    OperationStatusResponse,
    PathIngestRequest,
    RuntimeStatusResponse,
    SectionResponse,
)

try:
    from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError as exc:  # pragma: no cover - exercised by optional dependency installation
    raise ImportError(
        "FastAPI support requires the optional dependencies: pip install noomexai-raven[server]"
    ) from exc


@dataclass(slots=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 0
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    cors_origins: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")
    allowed_roots: tuple[Path, ...] = ()
    upload_dir: Path | None = None
    max_upload_bytes: int = 100 * 1024 * 1024
    event_history_size: int = 500
    raven_kwargs: dict[str, Any] = field(default_factory=dict)


class ServerRuntime:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        settings.allowed_roots = tuple(Path(root).resolve() for root in settings.allowed_roots)
        self.bus = EventBus(history_size=settings.event_history_size)
        self.operations = OperationManager(self.bus, history_size=settings.event_history_size)
        self.raven = Raven(bus=self.bus, **settings.raven_kwargs)
        self._initialize_lock = asyncio.Lock()
        self._initialize_operation_id: str | None = None
        self._state = "live"
        self._error: str | None = None
        upload_dir = settings.upload_dir
        self.upload_dir = (upload_dir or Path.cwd() / "data" / "uploads").resolve()
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    @property
    def ready(self) -> bool:
        return self.raven.is_started

    def status(self) -> RuntimeStatusResponse:
        return RuntimeStatusResponse(
            live=True,
            ready=self.ready,
            state="ready" if self.ready else self._state,
            operation_id=self._initialize_operation_id,
            error=self._error,
        )

    async def initialize(self, op_id: str) -> None:
        async with self._initialize_lock:
            if self.raven.is_started:
                self._state = "ready"
                return
            self._state = "initializing"
            self._initialize_operation_id = op_id
            try:
                await self.raven.start(op_id=op_id)
                self._state = "ready"
                self._error = None
            except Exception as exc:
                self._state = "failed"
                self._error = str(exc)
                raise

    async def ensure_ready(self, op_id: str) -> None:
        if not self.raven.is_started:
            await self.initialize(op_id)

    async def submit(self, work, *, terminal_events=None) -> OperationReference:
        record = await self.operations.submit(work, terminal_events=terminal_events)
        return OperationReference(
            operation_id=record.operation_id,
            status=record.status.value,
            events_url=f"/api/v1/operations/{record.operation_id}/events",
        )

    def allowed_path(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if not self.settings.allowed_roots:
            raise HTTPException(status_code=403, detail="trusted path ingestion is not configured")
        if not any(path == root or root in path.parents for root in self.settings.allowed_roots):
            raise HTTPException(status_code=403, detail="file path is outside allowed roots")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="file does not exist")
        return path

    async def close(self) -> None:
        await self.operations.close()
        await self.raven.close()
        self._state = "stopped"
        self.bus.close()


def _conversation_response(conversation) -> ConversationResponse:
    return ConversationResponse(**conversation.to_dict())


def _knowledge_response(row: dict[str, Any]) -> KnowledgeSummaryResponse:
    return KnowledgeSummaryResponse(**row)


def _event_envelope(event: Event) -> EventEnvelope:
    return EventEnvelope(
        event_id=event.event_id,
        operation_id=event.op_id,
        type=event.type.value,
        timestamp=event.ts,
        data=event.data,
    )


def _sse(event: Event) -> str:
    payload = _event_envelope(event).model_dump_json()
    event_name = event.type.value
    event_id = f"id: {event.event_id}\n" if event.event_id is not None else ""
    return f"{event_id}event: {event_name}\ndata: {payload}\n\n"


async def _event_stream(runtime: ServerRuntime, operation_id: str, after_event_id: int) -> AsyncIterator[str]:
    record = runtime.operations.get(operation_id)
    history = runtime.operations.history(operation_id, after_event_id)
    for event in history:
        yield _sse(event)
        after_event_id = event.event_id or after_event_id
        if event.type in record.terminal_events or event.type is EventType.ERROR:
            return

    if record.status in {
        OperationStatus.COMPLETED,
        OperationStatus.FAILED,
        OperationStatus.CANCELLED,
    }:
        return

    subscription = runtime.bus.subscribe(op_id=operation_id, after_event_id=after_event_id)
    iterator = subscription.__aiter__()
    pending_event = asyncio.create_task(iterator.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending_event}, timeout=15.0)
            if not done:
                yield ": heartbeat\n\n"
                continue
            try:
                event = pending_event.result()
            except StopAsyncIteration:
                return
            yield _sse(event)
            if event.type in record.terminal_events or event.type is EventType.ERROR:
                return
            pending_event = asyncio.create_task(iterator.__anext__())
    finally:
        if not pending_event.done():
            pending_event.cancel()
            await asyncio.gather(pending_event, return_exceptions=True)
        await subscription.aclose()


def create_app(settings: ServerSettings | None = None) -> FastAPI:
    settings = settings or ServerSettings()
    runtime = ServerRuntime(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await runtime.close()

    app = FastAPI(title="RAVEN API", version="0.2.0", lifespan=lifespan)
    app.state.runtime = runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    async def authorized(request: Request) -> None:
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(value, settings.token):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    @app.get("/api/v1/health/live", response_model=RuntimeStatusResponse)
    async def health_live() -> RuntimeStatusResponse:
        return runtime.status()

    @app.get("/api/v1/health/ready", response_model=RuntimeStatusResponse, dependencies=[Depends(authorized)])
    async def health_ready() -> RuntimeStatusResponse:
        status = runtime.status()
        if not status.ready:
            raise HTTPException(status_code=503, detail=status.model_dump())
        return status

    @app.get("/api/v1/runtime", response_model=RuntimeStatusResponse, dependencies=[Depends(authorized)])
    async def runtime_status() -> RuntimeStatusResponse:
        return runtime.status()

    @app.post("/api/v1/runtime/initialize", response_model=OperationReference, status_code=202, dependencies=[Depends(authorized)])
    async def runtime_initialize() -> OperationReference:
        return await runtime.submit(runtime.initialize)

    @app.get("/api/v1/models", response_model=list[ModelSummaryResponse], dependencies=[Depends(authorized)])
    async def list_models() -> list[ModelSummaryResponse]:
        await runtime.ensure_ready(uuid4().hex)
        return [ModelSummaryResponse(**row) for row in await runtime.raven.list_models()]

    @app.post("/api/v1/models/{model:path}/pull", response_model=OperationReference, status_code=202, dependencies=[Depends(authorized)])
    async def pull_model(model: str) -> OperationReference:
        async def work(op_id: str):
            await runtime.ensure_ready(op_id)
            await runtime.raven.pull_foreground(model, op_id)

        return await runtime.submit(work)

    @app.get("/api/v1/models/{model:path}", response_model=ModelInspectResponse, dependencies=[Depends(authorized)])
    async def inspect_model(model: str) -> ModelInspectResponse:
        await runtime.ensure_ready(uuid4().hex)
        return ModelInspectResponse(**(await runtime.raven.inspect(model)))

    @app.delete("/api/v1/models/{model:path}", status_code=204, dependencies=[Depends(authorized)])
    async def delete_model(model: str) -> None:
        await runtime.ensure_ready(uuid4().hex)
        await runtime.raven.delete_model(model)

    @app.get("/api/v1/knowledges", response_model=list[KnowledgeSummaryResponse], dependencies=[Depends(authorized)])
    async def list_knowledges() -> list[KnowledgeSummaryResponse]:
        await runtime.ensure_ready(uuid4().hex)
        rows = await asyncio.to_thread(runtime.raven.list_knowledges)
        return [_knowledge_response(row) for row in rows]

    @app.post("/api/v1/knowledges", response_model=KnowledgeSummaryResponse, status_code=201, dependencies=[Depends(authorized)])
    async def create_knowledge(payload: KnowledgeCreateRequest) -> KnowledgeSummaryResponse:
        await runtime.ensure_ready(uuid4().hex)
        knowledge = await runtime.raven.create_knowledge(payload.name, payload.user_summary)
        rows = await asyncio.to_thread(runtime.raven.list_knowledges)
        row = next(row for row in rows if row["name"] == knowledge.name)
        return _knowledge_response(row)

    @app.delete("/api/v1/knowledges/{name}", status_code=204, dependencies=[Depends(authorized)])
    async def delete_knowledge(name: str) -> None:
        await runtime.ensure_ready(uuid4().hex)
        await runtime.raven.delete_knowledge(name)

    @app.get("/api/v1/knowledges/{name}/files", response_model=list[FileResponse], dependencies=[Depends(authorized)])
    async def list_files(name: str) -> list[FileResponse]:
        await runtime.ensure_ready(uuid4().hex)
        return [FileResponse(**row) for row in await asyncio.to_thread(runtime.raven.list_files, name)]

    @app.get("/api/v1/knowledges/{name}/files/{file_name}/sections", response_model=list[SectionResponse], dependencies=[Depends(authorized)])
    async def list_sections(name: str, file_name: str) -> list[SectionResponse]:
        await runtime.ensure_ready(uuid4().hex)
        rows = await asyncio.to_thread(runtime.raven.list_sections, name, file_name)
        return [SectionResponse(**row) for row in rows]

    @app.get(
        "/api/v1/knowledges/{name}/files/{file_name}/sections/{section_id}",
        response_model=SectionResponse,
        dependencies=[Depends(authorized)],
    )
    async def get_section(name: str, file_name: str, section_id: str) -> SectionResponse:
        await runtime.ensure_ready(uuid4().hex)
        row = await asyncio.to_thread(runtime.raven.get_section, name, section_id)
        if row is None or row.get("file_name") != file_name:
            raise HTTPException(status_code=404, detail="section does not exist in this file")
        return SectionResponse(**row)

    @app.post("/api/v1/knowledges/{name}/files", response_model=OperationReference, status_code=202, dependencies=[Depends(authorized)])
    async def ingest_upload(name: str, upload: UploadFile = File(...)) -> OperationReference:
        filename = Path(upload.filename or "upload.txt").name
        if not filename or filename in {".", ".."}:
            raise HTTPException(status_code=400, detail="invalid upload filename")
        content = await upload.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="upload exceeds configured size limit")
        path = runtime.upload_dir / f"{uuid4().hex}-{filename}"
        await asyncio.to_thread(path.write_bytes, content)

        async def work(op_id: str):
            try:
                return await runtime.raven.ingest_foreground(name, str(path), op_id)
            finally:
                path.unlink(missing_ok=True)

        return await runtime.submit(work)

    @app.post("/api/v1/knowledges/{name}/ingest-path", response_model=OperationReference, status_code=202, dependencies=[Depends(authorized)])
    async def ingest_path(name: str, payload: PathIngestRequest) -> OperationReference:
        path = runtime.allowed_path(payload.file_path)

        async def work(op_id: str):
            return await runtime.raven.ingest_foreground(name, str(path), op_id)

        return await runtime.submit(work)

    @app.get("/api/v1/conversations", response_model=list[ConversationResponse], dependencies=[Depends(authorized)])
    async def list_conversations() -> list[ConversationResponse]:
        await runtime.ensure_ready(uuid4().hex)
        rows = await asyncio.to_thread(runtime.raven.list_conversations)
        return [ConversationResponse(**row) for row in rows]

    @app.post("/api/v1/conversations", response_model=ConversationResponse, status_code=201, dependencies=[Depends(authorized)])
    async def create_conversation(payload: ConversationCreateRequest) -> ConversationResponse:
        await runtime.ensure_ready(uuid4().hex)
        conversation = await runtime.raven.create_conversation(payload.knowledge_name)
        return _conversation_response(conversation)

    @app.get("/api/v1/conversations/{conversation_id}", response_model=ConversationResponse, dependencies=[Depends(authorized)])
    async def get_conversation(conversation_id: str) -> ConversationResponse:
        await runtime.ensure_ready(uuid4().hex)
        return _conversation_response(await asyncio.to_thread(runtime.raven.get_conversation, conversation_id))

    @app.patch("/api/v1/conversations/{conversation_id}", response_model=ConversationResponse, dependencies=[Depends(authorized)])
    async def patch_conversation(conversation_id: str, payload: ConversationPatchRequest) -> ConversationResponse:
        await runtime.ensure_ready(uuid4().hex)
        if payload.title is not None:
            conversation = await runtime.raven.rename_conversation(conversation_id, payload.title)
        else:
            conversation = await asyncio.to_thread(runtime.raven.get_conversation, conversation_id)
        if payload.pinned is not None and conversation.pinned != payload.pinned:
            conversation = await runtime.raven.toggle_pin(conversation_id)
        return _conversation_response(conversation)

    @app.delete("/api/v1/conversations/{conversation_id}", status_code=204, dependencies=[Depends(authorized)])
    async def delete_conversation(conversation_id: str) -> None:
        await runtime.ensure_ready(uuid4().hex)
        await runtime.raven.delete_conversation(conversation_id)

    @app.post("/api/v1/conversations/{conversation_id}/turns", response_model=OperationReference, status_code=202, dependencies=[Depends(authorized)])
    async def chat_turn(conversation_id: str, payload: ChatTurnRequest) -> OperationReference:
        async def work(op_id: str):
            await runtime.ensure_ready(op_id)
            reply = ""
            async for event in runtime.raven.stream(
                payload.user_text,
                conversation_id=conversation_id,
                knowledge_name=payload.knowledge_name,
                retrieval_mode=payload.retrieval_mode,
                op_id=op_id,
            ):
                if event.type is EventType.CHAT_COMPLETE:
                    reply = event.data.get("reply", "")
            return {"reply": reply}

        return await runtime.submit(work)

    @app.get("/api/v1/operations/{operation_id}", response_model=OperationStatusResponse, dependencies=[Depends(authorized)])
    async def operation_status(operation_id: str) -> OperationStatusResponse:
        try:
            return OperationStatusResponse(**runtime.operations.get(operation_id).to_dict())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.get("/api/v1/operations/{operation_id}/events", dependencies=[Depends(authorized)])
    async def operation_events(operation_id: str, request: Request) -> StreamingResponse:
        try:
            record = runtime.operations.get(operation_id)
            last_header = request.headers.get("last-event-id", "0")
            try:
                last_event_id = max(0, int(last_header))
            except ValueError:
                raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from None
            oldest, _latest = runtime.operations.history_bounds(operation_id)
            if oldest is not None and oldest > last_event_id + 1:
                raise HTTPException(status_code=410, detail="requested event history is no longer retained")
            return StreamingResponse(
                _event_stream(runtime, operation_id, last_event_id),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post("/api/v1/operations/{operation_id}/cancel", response_model=OperationStatusResponse, dependencies=[Depends(authorized)])
    async def cancel_operation(operation_id: str) -> OperationStatusResponse:
        try:
            record = await runtime.operations.cancel(operation_id)
            return OperationStatusResponse(**record.to_dict())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.exception_handler(KeyError)
    async def key_error_handler(_request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    return app
