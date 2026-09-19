"""Shared HTTP response contracts for the Raven server."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from llama_index.core.base.llms.types import ToolCallBlock
from llama_index.core.llms import ChatMessage

from ..agent.policy import RetrievalMode
from ..core.errors import ErrorCode
from ..core.events import Event, EventType
from ..core.operations import (
    OperationRecord,
    OperationStatus,
    OperationTask,
    OperationTaskRecord,
    RetryPolicy,
)
from ..providers import ModelRole, ModelSpec


MAX_IDENTIFIER_LENGTH = 255
MAX_TITLE_LENGTH = 500
MAX_SUMMARY_LENGTH = 32_768
MAX_USER_QUERY_LENGTH = 32_768
MAX_PREFERENCE_LENGTH = 8_192
MAX_SOURCE_PATH_LENGTH = 32_768
MAX_MODEL_OPTIONS = 128


class HealthResponse(BaseModel):
    """Minimal process health response used by foundational probes."""

    model_config = ConfigDict(extra="forbid")

    status: str




class LoadedModelResponse(BaseModel):
    """Safe identity of one model configured on a Raven runtime."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    role: ModelRole




class RuntimeModelsResponse(BaseModel):
    """Models currently configured for one user runtime."""

    model_config = ConfigDict(extra="forbid")

    llm: LoadedModelResponse | None
    embedding: LoadedModelResponse | None




class RuntimeStatusResponse(BaseModel):
    """Public state of one user-scoped Raven runtime."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    started: bool
    closed: bool
    models_loaded: bool
    operation_store_healthy: bool
    runtime_config_revision: int = Field(ge=0)
    models: RuntimeModelsResponse




class DiscoveryIssueResponse(BaseModel):
    """One persisted resource that could not be loaded safely."""

    model_config = ConfigDict(extra="forbid")

    resource_type: Literal["knowledge", "conversation"]
    directory_name: str
    code: ErrorCode
    message: str




class DiscoveryIssuesResponse(BaseModel):
    """All resource-discovery problems visible to one runtime."""

    model_config = ConfigDict(extra="forbid")

    items: list[DiscoveryIssueResponse]




class ModelSpecRequest(BaseModel):
    """Provider-neutral model configuration accepted over HTTP."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    model: str = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    role: ModelRole
    api_key_ref: str | None = Field(
        default=None,
        max_length=MAX_IDENTIFIER_LENGTH,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    options: dict[str, Any] = Field(default_factory=dict, max_length=MAX_MODEL_OPTIONS)


    @field_validator("provider", "model")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value cannot be empty")
        return value


    def to_domain(self) -> ModelSpec:
        return ModelSpec.model_validate(self.model_dump())




class ModelConfigureRequest(BaseModel):
    """LLM and embedding model pair configured for one Raven runtime."""

    model_config = ConfigDict(extra="forbid")

    llm: ModelSpecRequest
    embedding: ModelSpecRequest


    @model_validator(mode="after")
    def validate_roles(self) -> "ModelConfigureRequest":
        if self.llm.role != ModelRole.LLM:
            raise ValueError("llm must use role='llm'")
        if self.embedding.role != ModelRole.EMBEDDING:
            raise ValueError("embedding must use role='embedding'")
        return self




class OllamaModelRequest(BaseModel):
    """One Ollama model name used by a model action."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)


    @field_validator("model")
    @classmethod
    def strip_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("model cannot be empty")
        return value



class ConfiguredModelPreloadRequest(BaseModel):
    """Optional residency duration for the configured local model."""

    model_config = ConfigDict(extra="forbid")

    keep_alive: float | str | None = None


    @field_validator("keep_alive", mode="before")
    @classmethod
    def validate_keep_alive(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("keep_alive must be a duration string or finite number")
        if isinstance(value, str):
            if not value or value.strip() != value:
                raise ValueError("keep_alive cannot be empty or padded")
            if value == "0":
                raise ValueError("keep_alive cannot be zero for preload")
            return value
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("keep_alive must be a duration string or finite number")
        if value == 0:
            raise ValueError("keep_alive cannot be zero for preload")
        return value




class OllamaProviderStatusResponse(BaseModel):
    """Availability of the configured external Ollama service."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["ollama"] = "ollama"
    status: Literal["available"] = "available"




class OllamaModelDetailsResponse(BaseModel):
    """Stable subset of Ollama's model-detail metadata."""

    model_config = ConfigDict(extra="forbid")

    parent_model: str | None = None
    format: str | None = None
    family: str | None = None
    families: list[str] | None = None
    parameter_size: str | None = None
    quantization_level: str | None = None




class OllamaModelResponse(BaseModel):
    """One model installed in the configured Ollama service."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    modified_at: datetime | None = None
    digest: str | None = None
    size: int | None = Field(default=None, ge=0)
    details: OllamaModelDetailsResponse | None = None




class OllamaModelsResponse(BaseModel):
    """Installed Ollama models."""

    model_config = ConfigDict(extra="forbid")

    items: list[OllamaModelResponse]




class OllamaModelInspectionResponse(BaseModel):
    """Typed Ollama inspection result for one requested model."""

    model_config = ConfigDict(extra="forbid")

    model: str
    modified_at: datetime | None = None
    template: str | None = None
    modelfile: str | None = None
    license: str | None = None
    details: OllamaModelDetailsResponse | None = None
    model_info: dict[str, Any] = Field(default_factory=dict)
    parameters: str | None = None
    capabilities: list[str] = Field(default_factory=list)




class KnowledgeCreateRequest(BaseModel):
    """Create a knowledge database with a user-facing summary."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    user_summary: str = Field(default="", max_length=MAX_SUMMARY_LENGTH)




class KnowledgeUpdateRequest(BaseModel):
    """Replace a knowledge database's user-facing summary."""

    model_config = ConfigDict(extra="forbid")

    user_summary: str = Field(max_length=MAX_SUMMARY_LENGTH)




class IngestPathRequest(BaseModel):
    """An administrator-trusted local source path; browser clients use uploads."""

    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(min_length=1, max_length=MAX_SOURCE_PATH_LENGTH)




class KnowledgeDetailsResponse(BaseModel):
    """Persisted metadata of one knowledge database."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    name: str
    created_at: datetime
    user_summary: str




class KnowledgeSummaryResponse(BaseModel):
    """Bounded list representation of one knowledge database."""

    model_config = ConfigDict(extra="forbid")

    name: str
    user_summary: str
    count: int = Field(ge=0)
    created_at: datetime




class KnowledgePageResponse(BaseModel):
    """One bounded page of knowledge summaries."""

    model_config = ConfigDict(extra="forbid")

    items: list[KnowledgeSummaryResponse]
    next_cursor: str | None = None




class KnowledgeFileResponse(BaseModel):
    """Metadata of an ingested file, including file-level navigation type."""

    model_config = ConfigDict(extra="forbid")

    file_id: str
    file_name: str
    section_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    ingested_at: datetime
    navigation_type: str




class KnowledgeFilePageResponse(BaseModel):
    """One bounded page of files belonging to a knowledge database."""

    model_config = ConfigDict(extra="forbid")

    items: list[KnowledgeFileResponse]
    next_cursor: str | None = None




class KnowledgeSectionResponse(BaseModel):
    """Full stored section evidence and provenance metadata."""

    model_config = ConfigDict(extra="forbid")

    section_id: str
    file_id: str
    file_name: str
    section_index: int = Field(ge=1)
    summary: str
    keywords: list[str]
    conditions: list[str]
    definitions: list[str]
    raw_content: str
    source_element_ids: list[str]
    source_range: list[int] | None




class KnowledgeSectionsResponse(BaseModel):
    """Sections of one named file."""

    model_config = ConfigDict(extra="forbid")

    items: list[KnowledgeSectionResponse]
    next_cursor: int | None = None




class KnowledgeStatsResponse(BaseModel):
    """Vector and file counts for a knowledge database."""

    model_config = ConfigDict(extra="forbid")

    vector_count: int = Field(ge=0)
    file_count: int = Field(ge=0)




class ConversationCreateRequest(BaseModel):
    """Create a global chat, or a local chat scoped to one knowledge."""

    model_config = ConfigDict(extra="forbid")

    knowledge_name: str | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)




class ConversationUpdateRequest(BaseModel):
    """Update one or both user-editable conversation fields."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    pinned: bool | None = None


    @model_validator(mode="after")
    def require_change(self) -> "ConversationUpdateRequest":
        if self.title is None and self.pinned is None:
            raise ValueError("title or pinned must be supplied")
        return self




class ConversationResponse(BaseModel):
    """Persisted conversation metadata with backend-computed scope type."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    conversation_id: str
    type: Literal["local", "global"]
    knowledge_name: str | None
    title: str
    is_titled: bool
    pinned: bool
    created_at: datetime




class ConversationPageResponse(BaseModel):
    """Newest-first page of conversation metadata."""

    model_config = ConfigDict(extra="forbid")

    items: list[ConversationResponse]
    next_cursor: str | None = None




class MessageToolCallResponse(BaseModel):
    """A tool call persisted as an assistant message block."""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)




class ConversationMessageResponse(BaseModel):
    """One canonical, uncompacted transcript message."""

    model_config = ConfigDict(extra="forbid")

    message_id: int = Field(ge=1)
    turn_id: UUID
    message_order: int = Field(ge=0)
    role: str
    content: str | None
    tool_calls: list[MessageToolCallResponse] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None


    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "ConversationMessageResponse":
        message: ChatMessage = record["message"]
        kwargs = message.additional_kwargs
        return cls(
            message_id=record["message_id"],
            turn_id=record["turn_id"],
            message_order=record["message_order"],
            role=message.role.value,
            content=message.content if isinstance(message.content, str) else None,
            tool_calls=[
                MessageToolCallResponse(
                    call_id=block.tool_call_id,
                    name=block.tool_name,
                    arguments=block.tool_kwargs,
                )
                for block in message.blocks
                if isinstance(block, ToolCallBlock)
            ],
            tool_call_id=(
                kwargs.get("tool_call_id")
                if isinstance(kwargs.get("tool_call_id"), str)
                else None
            ),
            tool_name=(
                kwargs.get("tool_name")
                if isinstance(kwargs.get("tool_name"), str)
                else None
            ),
        )




class ConversationMessagePageResponse(BaseModel):
    """A cursor page of canonical transcript messages."""

    model_config = ConfigDict(extra="forbid")

    items: list[ConversationMessageResponse]
    next_cursor: int | None = None




class TurnCreateRequest(BaseModel):
    """Submit a user query and optional retrieval-tool restriction."""

    model_config = ConfigDict(extra="forbid")

    user_query: str = Field(min_length=1, max_length=MAX_USER_QUERY_LENGTH)
    retrieval_mode: RetrievalMode | Literal["auto"] | None = None


    @field_validator("user_query")
    @classmethod
    def validate_user_query(cls, value: str) -> str:
        query = value.strip()
        if not query:
            raise ValueError("user_query must contain non-whitespace characters")
        return query




class TurnAcceptedResponse(BaseModel):
    """Reference returned immediately for one streamed conversation turn."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    turn_id: UUID
    operation_id: UUID
    task_id: UUID
    status: OperationStatus
    events_url: str




class EvidenceReferenceResponse(BaseModel):
    """One verifiable section reference in a completed turn."""

    model_config = ConfigDict(extra="forbid")

    knowledge_name: str
    file_name: str
    section_id: str




class ReconstructedSectionResponse(BaseModel):
    """One stored section in a highlighted reconstructed source view."""

    model_config = ConfigDict(extra="forbid")

    section_id: str
    highlighted: bool
    raw_content: str
    source_range: list[int] | None = None




class ReconstructedFileResponse(BaseModel):
    """A file view emitted by the reconstructor."""

    model_config = ConfigDict(extra="forbid")

    knowledge_name: str
    file_name: str
    navigation_type: str
    sections: list[ReconstructedSectionResponse]




class TurnToolCallResponse(BaseModel):
    """A correlated tool call and its bounded UI-visible outcome."""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    step: int | None = None
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool | None = None
    ui_summary: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None




class TurnResultResponse(BaseModel):
    """Completed turn projection derived from the operation event stream."""

    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    thinking: str = ""
    response: str
    iteration_limit_reached: bool = False
    evidence: list[EvidenceReferenceResponse] = Field(default_factory=list)
    tool_calls: list[TurnToolCallResponse] = Field(default_factory=list)
    reconstructed_sources: list[ReconstructedFileResponse] = Field(default_factory=list)




class TurnResponse(BaseModel):
    """One durable canonical turn, distinct from an in-flight operation."""

    model_config = ConfigDict(extra="forbid")

    turn_id: UUID
    operation_id: UUID | None
    user_query: str
    result: TurnResultResponse | None
    memory_indexed: bool
    committed_at: datetime




class PreferenceCreateRequest(BaseModel):
    """One explicit conversation-scoped user preference."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_PREFERENCE_LENGTH)




class PreferenceResponse(BaseModel):
    """Persisted preference with an exact deletion identity."""

    model_config = ConfigDict(extra="forbid")

    preference_id: UUID
    text: str




class PreferencesResponse(BaseModel):
    """All bounded explicit preferences of one conversation."""

    model_config = ConfigDict(extra="forbid")

    items: list[PreferenceResponse]




class ErrorBody(BaseModel):
    """Machine-readable error information safe for API consumers."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)




class ErrorResponse(BaseModel):
    """Uniform envelope returned by every HTTP error handler."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody
    request_id: str




class OperationResponse(BaseModel):
    """Public status for one durable operation."""

    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    name: str
    status: OperationStatus
    last_event_id: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None
    is_finished: bool


    @classmethod
    def from_record(cls, record: OperationRecord) -> "OperationResponse":
        return cls(
            **record.model_dump(),
            is_finished=record.is_finished,
        )




class OperationPageResponse(BaseModel):
    """One bounded page of operations in newest-first order."""

    model_config = ConfigDict(extra="forbid")

    items: list[OperationResponse]
    next_cursor: UUID | None = None




class OperationTaskResponse(BaseModel):
    """Public status for one task belonging to an operation."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    operation_id: UUID
    name: str
    is_root: bool
    status: OperationStatus
    retry_policy: RetryPolicy
    attempt: int
    max_attempts: int
    attempts_remaining: int
    can_retry: bool
    retry_of_operation_id: UUID | None
    retry_of_task_id: UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None


    @classmethod
    def from_record(cls, record: OperationTaskRecord) -> "OperationTaskResponse":
        values = record.model_dump(exclude={"retry_input"})
        return cls(
            **values,
            max_attempts=record.max_attempts,
            attempts_remaining=record.attempts_remaining,
            can_retry=record.can_retry,
        )




class OperationTaskPageResponse(BaseModel):
    """One bounded page of operation tasks in newest-first order."""

    model_config = ConfigDict(extra="forbid")

    items: list[OperationTaskResponse]
    next_cursor: UUID | None = None




class OperationTaskReference(BaseModel):
    """Reference returned when a new retry task has been accepted."""

    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    task_id: UUID
    status: OperationStatus
    events_url: str
    retry_of_operation_id: UUID | None = None
    retry_of_task_id: UUID | None = None


    @classmethod
    def from_task(cls, task: OperationTask) -> "OperationTaskReference":
        return cls(
            operation_id=task.operation_id,
            task_id=task.task_id,
            status=task.status,
            events_url=f"/api/v1/operations/{task.operation_id}/events",
            retry_of_operation_id=task.retry_of_operation_id,
            retry_of_task_id=task.retry_of_task_id,
        )




class SSEEventEnvelope(BaseModel):
    """JSON payload carried by one typed SSE event frame."""

    model_config = ConfigDict(extra="forbid")

    event_id: int
    operation_id: UUID
    task_id: UUID | None
    task_name: str | None
    type: EventType
    timestamp: datetime
    data: dict[str, Any]
    is_final: bool


    @classmethod
    def from_event(cls, event: Event) -> "SSEEventEnvelope":
        if event.event_id is None or event.operation_id is None:
            raise ValueError("Only persisted operation events can be serialized.")
        return cls(
            event_id=event.event_id,
            operation_id=event.operation_id,
            task_id=event.task_id,
            task_name=event.task_name,
            type=event.type,
            timestamp=event.timestamp,
            data=event.data,
            is_final=event.is_final,
        )
