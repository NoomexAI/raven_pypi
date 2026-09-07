"""Transport-neutral Raven errors and stable error codes."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable machine-readable Raven error codes."""

    INVALID_KNOWLEDGE_NAME = "invalid_knowledge_name"
    KNOWLEDGE_ALREADY_EXISTS = "knowledge_already_exists"
    KNOWLEDGE_NOT_FOUND = "knowledge_not_found"
    KNOWLEDGE_CLOSED = "knowledge_closed"
    KNOWLEDGE_NOT_STARTED = "knowledge_not_started"
    CONVERSATION_ALREADY_EXISTS = "conversation_already_exists"
    CONVERSATION_NOT_FOUND = "conversation_not_found"
    CONVERSATION_CLOSED = "conversation_closed"
    CONVERSATION_NOT_STARTED = "conversation_not_started"
    CONVERSATION_MEMORY_NOT_INITIALIZED = "conversation_memory_not_initialized"
    CONVERSATION_TURN_CONFLICT = "conversation_turn_conflict"
    CONVERSATION_TURN_NOT_FOUND = "conversation_turn_not_found"
    CONVERSATION_TURN_RESULT_MISSING = "conversation_turn_result_missing"
    INVALID_PREFERENCE_ID = "invalid_preference_id"
    PREFERENCE_NOT_FOUND = "preference_not_found"
    INVALID_CONVERSATION_ID = "invalid_conversation_id"
    INVALID_CONVERSATION_TITLE = "invalid_conversation_title"
    INVALID_RETRIEVAL_MODE = "invalid_retrieval_mode"
    RETRIEVAL_MODE_NOT_ALLOWED = "retrieval_mode_not_allowed"
    FILE_ALREADY_EXISTS = "file_already_exists"
    FILE_NOT_FOUND = "file_not_found"
    SECTION_NOT_FOUND = "section_not_found"
    LLM_MODEL_REQUIRED = "llm_model_required"
    EMBEDDING_MODEL_REQUIRED = "embedding_model_required"
    INVALID_MODEL_SPEC = "invalid_model_spec"
    MODEL_API_KEY_NOT_FOUND = "model_api_key_not_found"
    MODEL_PROVIDER_FAILED = "model_provider_failed"
    AGENT_MAX_ITERATIONS = "agent_max_iterations"
    INVALID_CHUNKING = "invalid_chunking"
    NO_CHUNKS_PRODUCED = "no_chunks_produced"
    INVALID_EMBEDDING_RESULT = "invalid_embedding_result"
    EMBEDDING_DIMENSION_MISMATCH = "embedding_dimension_mismatch"
    SOURCE_FILE_NOT_FOUND = "source_file_not_found"
    SOURCE_FILE_CHANGED = "source_file_changed"
    SOURCE_FILE_UNREADABLE = "source_file_unreadable"
    UNSUPPORTED_SOURCE_FILE = "unsupported_source_file"
    DOCUMENT_PARSE_FAILED = "document_parse_failed"
    NO_SECTIONS_PRODUCED = "no_sections_produced"
    SECTION_METADATA_EXTRACTION_FAILED = "section_metadata_extraction_failed"
    INGESTION_RECONCILIATION_FAILED = "ingestion_reconciliation_failed"

    INVALID_OPERATION_NAME = "invalid_operation_name"
    OPERATION_NOT_FOUND = "operation_not_found"
    OPERATION_CANCELLED = "operation_cancelled"
    OPERATION_INTERRUPTED = "operation_interrupted"
    OPERATION_FINISHED = "operation_finished"
    INVALID_OPERATION_ID = "invalid_operation_id"
    OPERATION_MANAGER_CLOSED = "operation_manager_closed"
    OPERATION_TASK_NOT_FOUND = "operation_task_not_found"
    OPERATION_TASK_NOT_RETRYABLE = "operation_task_not_retryable"
    OPERATION_TASK_ALREADY_RETRIED = "operation_task_already_retried"
    INVALID_RETRY_INPUT = "invalid_retry_input"

    INVALID_METADATA = "invalid_metadata"
    UNSUPPORTED_METADATA_VERSION = "unsupported_metadata_version"
    PERSISTENCE_FAILED = "persistence_failed"

    EVENT_STREAM_CLOSED = "event_stream_closed"
    EVENT_STREAM_FINISHED = "event_stream_finished"
    INVALID_EVENT_CURSOR = "invalid_event_cursor"
    EVENT_HISTORY_GAP = "event_history_gap"
    OPERATION_SYNC_FAILED = "operation_sync_failed"
    OPERATION_DATABASE_FAILED = "operation_database_failed"
    OPERATION_DATABASE_CORRUPTED = "operation_database_corrupted"
    INVALID_OPERATION_SYNC_INTERVAL = "invalid_operation_sync_interval"
    INVALID_RETENTION = "invalid_retention"

    OLLAMA_UNAVAILABLE = "ollama_unavailable"
    OLLAMA_OPERATION_FAILED = "ollama_operation_failed"
    INTERNAL_ERROR = "internal_error"


class RavenError(Exception):
    """An expected Raven error safe to expose at an application boundary."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


def error_payload(error: BaseException) -> dict[str, Any]:
    """Return a safe event/API payload for an exception."""
    if isinstance(error, RavenError):
        return error.as_payload()
    return {
        "code": ErrorCode.INTERNAL_ERROR.value,
        "message": "An unexpected internal error occurred.",
    }
