from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class OperationReference(BaseModel):
    operation_id: str
    status: str
    events_url: str


class OperationStatusResponse(BaseModel):
    operation_id: str
    status: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    result: Any = None
    error: str | None = None


class EventEnvelope(BaseModel):
    event_id: int | None = None
    operation_id: str
    type: str
    timestamp: float
    data: dict[str, Any] = Field(default_factory=dict)


class RuntimeStatusResponse(BaseModel):
    live: bool
    ready: bool
    state: str
    operation_id: str | None = None
    error: str | None = None


class KnowledgeCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    user_summary: str = Field(default="", max_length=10_000)


class KnowledgeSummaryResponse(BaseModel):
    name: str
    safe_name: str
    user_summary: str
    count: int
    embed_model: str | None = None
    created_at: str | None = None


class FileResponse(BaseModel):
    file_id: str | None = None
    file_name: str
    section_count: int
    chunk_count: int
    ingested_at: float | None = None


class SectionResponse(BaseModel):
    section_id: str
    file_id: str
    file_name: str
    section_index: int
    summary: str = ""
    keywords: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    definitions: list[str] = Field(default_factory=list)
    raw_content: str = ""


class ConversationCreateRequest(BaseModel):
    knowledge_name: str | None = None


class ConversationPatchRequest(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    pinned: bool | None = None


class ConversationResponse(BaseModel):
    conversation_id: str
    type: str
    knowledge_name: str | None = None
    title: str
    pinned: bool
    created_at: str


class ChatTurnRequest(BaseModel):
    user_text: str = Field(min_length=1, max_length=100_000)
    retrieval_mode: str = "auto"
    knowledge_name: str | None = None


class PathIngestRequest(BaseModel):
    file_path: str


class ModelSummaryResponse(BaseModel):
    model: str
    modified_at: Any = None
    size: int | None = None
    digest: str | None = None
    details: dict[str, Any] | None = None

    model_config = {"extra": "allow"}


class ModelInspectResponse(BaseModel):
    model: str | None = None
    details: dict[str, Any] | None = None

    model_config = {"extra": "allow"}
