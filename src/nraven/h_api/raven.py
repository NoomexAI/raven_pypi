"""High-level library API for Raven."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from llama_index.core.base.llms.types import MessageRole
from llama_index.core.llms import ChatMessage

from ..agent.contracts import RetrievalPipelines
from ..agent.harness import AgentHarness
from ..agent.policy import GLOBAL_RETRIEVAL_MODES, LOCAL_RETRIEVAL_MODES, RetrievalMode
from ..core.async_utils import await_completion
from ..core.config import (
    DEFAULT_USER_ID,
    PathConfig,
    RuntimeConfig,
    SystemConfig,
)
from ..core.errors import ErrorCode, RavenError
from ..core.events import Event
from ..core.operations import (
    Operation,
    OperationManager,
    OperationRecord,
    OperationStatus,
    OperationTask,
    OperationTaskRecord,
    OperationType,
    OperationWorker,
    TaskWorker,
)
from ..core.upload_retention import check_upload_retry
from ..data_management.conversation_manager import Conversation, ConversationManager
from ..data_management.discovery import DiscoveryIssue
from ..data_management.knowledge_base import Knowledge, KnowledgeBase
from ..document_processing.document_parser import DocumentParser
from ..pipelines.ingestion import IngestionPipeline, IngestionRetryInput
from ..pipelines.reconstructor import Reconstructor
from ..pipelines.retrieval import (
    AgreementBasedRetrievalPipeline,
    EmbeddedRetrievalPipeline,
    HierarchicalRetrievalPipeline,
    VectorConditionedRetrievalPipeline,
)
from ..providers import ModelRole, ModelSpec, Provider
from ..providers.ollama_manager import ProgressCallback
from ..session.session import Session, SessionRetryInput


class Raven:
    """Compose Raven's components behind one operation-first API."""

    def __init__(
        self,
        raven_home: str | Path,
        user_id: UUID | str = DEFAULT_USER_ID,
        *,
        system_config: SystemConfig | None = None,
    ) -> None:
        self.system_config = system_config or SystemConfig()
        self.paths = PathConfig(
            Path(raven_home),
            user_id=user_id,
        )
        self._runtime_config = RuntimeConfig()
        self._runtime_config.validate_against(self.system_config)
        self.llm: Any | None = None
        self.embed_model: Any | None = None

        self.operation_manager = OperationManager(
            self.paths,
            sync_interval=self.system_config.operation_sync_interval_seconds,
            max_cached_finished_operations=(
                self.system_config.finished_operation_cache_size
            ),
            event_replay_page_size=self.system_config.event_replay_page_size,
        )
        self.provider = Provider(
            operation_manager=self.operation_manager,
            max_cached_llms=self.system_config.llm_adapter_cache_size,
            max_cached_embeddings=(
                self.system_config.embedding_adapter_cache_size
            ),
        )
        self.knowledge_base = KnowledgeBase(
            self.paths,
            self.operation_manager,
            max_open_knowledges=self.system_config.open_knowledge_limit,
        )
        self.conversation_manager = ConversationManager(
            self.paths,
            self.knowledge_base,
            self.operation_manager,
            max_open_conversations=self.system_config.open_conversation_limit,
        )
        self.reconstructor = Reconstructor(
            self.knowledge_base,
            self.operation_manager,
        )
        self._document_parser = DocumentParser()
        self.embedded_retrieval: EmbeddedRetrievalPipeline | None = None
        self.hierarchical_retrieval: HierarchicalRetrievalPipeline | None = None
        self.agreement_retrieval: AgreementBasedRetrievalPipeline | None = None
        self.vector_conditioned_retrieval: VectorConditionedRetrievalPipeline | None = None
        self.retrieval_pipelines: RetrievalPipelines | None = None
        self._sessions: dict[str, Session] = {}
        self._stale_sessions: set[str] = set()
        self._retired_sessions: dict[str, Session] = {}
        self._session_maintenance_tasks: set[asyncio.Task[None]] = set()
        self._session_registry_lock = threading.RLock()
        self._lifecycle_lock = asyncio.Lock()
        self._model_lock = asyncio.Lock()
        self._retry_lock = asyncio.Lock()
        self._runtime_config_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._llm_spec: ModelSpec | None = None
        self._embedding_spec: ModelSpec | None = None
        self._models_reloading = False
        self._started = False
        self._closed = False


    @property
    def is_started(self) -> bool:
        return self._started and not self._closed


    @property
    def models_loaded(self) -> bool:
        return self.llm is not None and self.embed_model is not None


    @property
    def runtime_config(self) -> RuntimeConfig:
        """Return the current immutable per-user configuration snapshot."""
        return self._runtime_config


    def runtime_status(self) -> dict[str, Any]:
        """Return a transport-safe snapshot of this user-scoped runtime."""
        return {
            "user_id": str(self.paths.user_id),
            "started": self.is_started,
            "closed": self._closed,
            "models_loaded": self.models_loaded,
            "operation_store_healthy": self.operation_manager.is_healthy,
            "runtime_config_revision": self._runtime_config.revision,
            "models": {
                "llm": (
                    self._model_result(self._llm_spec)
                    if self._llm_spec is not None
                    else None
                ),
                "embedding": (
                    self._model_result(self._embedding_spec)
                    if self._embedding_spec is not None
                    else None
                ),
            },
        }


    def get_runtime_settings(self) -> dict[str, Any]:
        """Return the current per-user runtime defaults as JSON-safe data."""
        return self._runtime_config.to_dict()


    async def update_runtime_settings(
        self,
        changes: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Atomically validate, persist, and install a runtime configuration."""
        active_operation = operation or await self.operation_manager.create(
            OperationType.RUNTIME_SETTINGS_UPDATE
        )
        return await active_operation.run(
            OperationType.RUNTIME_SETTINGS_UPDATE,
            lambda _operation: self._update_runtime_settings(
                changes,
                expected_revision=expected_revision,
            ),
        )


    async def _update_runtime_settings(
        self,
        changes: Mapping[str, Any],
        *,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        payload = dict(changes)
        async with self._runtime_config_lock:
            current = self._runtime_config
            self._check_runtime_revision(expected_revision, current.revision)
            updated = current.updated(payload)
            updated.validate_against(self.system_config)
            changed_fields = {
                name
                for name in payload
                if getattr(current, name) != getattr(updated, name)
            }
            if not changed_fields:
                return current.to_dict()
            await self._commit_runtime_config(updated, changed_fields)
            return updated.to_dict()


    async def reset_runtime_settings(
        self,
        *,
        expected_revision: int | None = None,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Restore built-in runtime defaults as a new persisted revision."""
        active_operation = operation or await self.operation_manager.create(
            OperationType.RUNTIME_SETTINGS_RESET
        )
        return await active_operation.run(
            OperationType.RUNTIME_SETTINGS_RESET,
            lambda _operation: self._reset_runtime_settings(expected_revision),
        )


    async def _reset_runtime_settings(
        self,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        async with self._runtime_config_lock:
            current = self._runtime_config
            self._check_runtime_revision(expected_revision, current.revision)
            defaults = RuntimeConfig.from_dict(
                {**RuntimeConfig().to_dict(), "revision": current.revision + 1}
            )
            defaults.validate_against(self.system_config)
            changed_fields = {
                field_name
                for field_name, value in defaults.to_dict().items()
                if field_name not in {"schema_version", "revision"}
                and getattr(current, field_name) != value
            }
            await self._commit_runtime_config(defaults, changed_fields)
            return defaults.to_dict()


    async def _commit_runtime_config(
        self,
        config: RuntimeConfig,
        changed_fields: set[str],
    ) -> None:
        async def commit() -> None:
            await asyncio.to_thread(config.save, self.paths.runtime_settings_path)
            self._runtime_config = config
            await self._invalidate_runtime_sessions(changed_fields)

        commit_task = asyncio.create_task(commit())
        _, cancellation_requested = await await_completion(commit_task)
        if cancellation_requested:
            raise asyncio.CancelledError


    @staticmethod
    def _check_runtime_revision(expected: int | None, current: int) -> None:
        if expected is None:
            return
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise RavenError(
                ErrorCode.INVALID_RUNTIME_CONFIG,
                "expected_revision must be a non-negative integer.",
            )
        if expected != current:
            raise RavenError(
                ErrorCode.RUNTIME_CONFIG_CONFLICT,
                "The runtime configuration changed before this update was applied.",
                details={
                    "expected_revision": expected,
                    "current_revision": current,
                },
            )


    async def _invalidate_runtime_sessions(self, changed_fields: set[str]) -> None:
        session_fields = {
            "retrieval_top_k",
            "agent_max_iterations",
            "memory_token_limit",
            "memory_top_k",
        }
        if not changed_fields.intersection(session_fields):
            return
        sessions: list[Session] = []
        with self._session_registry_lock:
            for conversation_id, session in list(self._sessions.items()):
                if session.has_active_runs:
                    self._stale_sessions.add(conversation_id)
                    continue
                self._sessions.pop(conversation_id, None)
                sessions.append(session)
        results = await asyncio.gather(
            *(session.close() for session in sessions),
            return_exceptions=True,
        )
        with self._session_registry_lock:
            self._retired_sessions.update(
                {
                    session.conversation_id: session
                    for session, result in zip(sessions, results)
                    if isinstance(result, BaseException)
                }
            )


    def _session_became_idle(self, session: Session) -> None:
        conversation_id = session.conversation_id
        with self._session_registry_lock:
            if conversation_id not in self._stale_sessions:
                return
            if self._sessions.get(conversation_id) is not session:
                self._stale_sessions.discard(conversation_id)
                return
            self._sessions.pop(conversation_id, None)
            self._stale_sessions.discard(conversation_id)
            self._retired_sessions[conversation_id] = session
        task = asyncio.create_task(
            self._retire_session(session),
            name=f"nraven-stale-session-close-{conversation_id}",
        )
        self._session_maintenance_tasks.add(task)
        task.add_done_callback(self._session_maintenance_tasks.discard)


    async def _retire_session(self, session: Session) -> None:
        try:
            await session.close()
        except BaseException:
            return
        with self._session_registry_lock:
            if self._retired_sessions.get(session.conversation_id) is session:
                self._retired_sessions.pop(session.conversation_id, None)


    async def submit_operation(
        self,
        name: str,
        worker: OperationWorker,
    ) -> Operation:
        """Create and start a root operation from a worker."""
        operation = await self.operation_manager.create(name)
        await operation.run(name, worker)
        return operation


    async def create_operation(self, name: str) -> Operation:
        """Create an operation whose tasks can be run by the caller."""
        return await self.operation_manager.create(name)


    async def run_operation(
        self,
        name: str,
        worker: TaskWorker,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Run a root task or a child task in a supplied operation."""
        active_operation = operation or await self.operation_manager.create(name)
        return await active_operation.run(
            name,
            worker,
        )


    async def get_operation(self, operation_id: UUID | str) -> Operation:
        return await self.operation_manager.get(operation_id)


    async def get_operation_record(
        self,
        operation_id: UUID | str,
    ) -> OperationRecord:
        return await self.operation_manager.get_record(operation_id)


    async def get_operation_task(
        self,
        operation_id: UUID | str,
        task_id: UUID | str,
    ) -> OperationTaskRecord:
        operation = await self.operation_manager.get(operation_id)
        return await operation.get_task(task_id)


    async def list_operations(
        self,
        *,
        status: OperationStatus | str | None = None,
        limit: int | None = None,
        after_operation_id: UUID | str | None = None,
    ) -> list[OperationRecord]:
        return await self.operation_manager.list_operations(
            status=status,
            limit=self.system_config.operation_page_size if limit is None else limit,
            after_operation_id=after_operation_id,
        )


    async def list_operation_tasks(
        self,
        operation_id: UUID | str,
        *,
        status: OperationStatus | str | None = None,
        limit: int | None = None,
        after_task_id: UUID | str | None = None,
    ) -> list[OperationTaskRecord]:
        operation = await self.operation_manager.get(operation_id)
        return await operation.list_tasks(
            status=status,
            limit=self.system_config.operation_page_size if limit is None else limit,
            after_task_id=after_task_id,
        )


    async def list_retryable_tasks(
        self,
        *,
        limit: int | None = None,
        after_task_id: UUID | str | None = None,
    ) -> list[OperationTaskRecord]:
        return await self.operation_manager.list_retryable_tasks(
            limit=self.system_config.operation_page_size if limit is None else limit,
            after_task_id=after_task_id,
        )


    async def retry_task(
        self,
        operation_id: UUID | str,
        task_id: UUID | str,
    ) -> OperationTask:
        """Start a user-confirmed retry as a new linked operation."""
        async with self._retry_lock:
            return await self._retry_task(operation_id, task_id)


    async def _retry_task(
        self,
        operation_id: UUID | str,
        task_id: UUID | str,
    ) -> OperationTask:
        operation = await self.operation_manager.get(operation_id)
        failed_task = await operation.get_task(task_id)
        existing_retry = await operation.get_retry(task_id)
        if existing_retry is not None:
            raise RavenError(
                ErrorCode.OPERATION_TASK_ALREADY_RETRIED,
                f"Operation task '{failed_task.task_id}' already has a retry.",
                details={
                    "retry_task_id": str(existing_retry.task_id),
                    "retry_operation_id": str(existing_retry.operation_id),
                    "retry_status": existing_retry.status.value,
                },
            )
        if not failed_task.can_retry:
            raise RavenError(
                ErrorCode.OPERATION_TASK_NOT_RETRYABLE,
                f"Operation task '{failed_task.task_id}' is not retryable.",
                details={
                    "status": failed_task.status.value,
                    "attempt": failed_task.attempt,
                    "max_attempts": failed_task.max_attempts,
                    "attempts_remaining": failed_task.attempts_remaining,
                    "error_code": (
                        failed_task.error.get("code")
                        if failed_task.error is not None
                        else None
                    ),
                },
            )

        if failed_task.name == OperationType.INGESTION_RUN.value:
            self._ensure_models_loaded()
            assert self.llm is not None
            assert self.embed_model is not None
            try:
                retry_input = IngestionRetryInput.model_validate(
                    failed_task.retry_input
                )
            except ValidationError as exc:
                raise RavenError(
                    ErrorCode.INVALID_RETRY_INPUT,
                    "The ingestion retry input is invalid.",
                ) from exc
            await asyncio.to_thread(
                check_upload_retry,
                Path(retry_input.source_path),
                self.paths.uploads_dir,
            )
            pipeline = IngestionPipeline(
                self.knowledge_base,
                self.operation_manager,
                breakpoint_percentile_threshold=(
                    retry_input.breakpoint_percentile_threshold
                ),
                buffer_size=retry_input.buffer_size,
                max_extraction_retries=retry_input.max_extraction_retries,
                document_parser=self._document_parser,
            )
            return await pipeline.run(
                retry_input.knowledge_name,
                retry_input.source_path,
                llm=self.llm,
                embed_model=self.embed_model,
                retry_of=failed_task,
                chunk_size=retry_input.chunk_size,
                chunk_overlap=retry_input.chunk_overlap,
            )

        if failed_task.name == OperationType.RECONSTRUCTION_RECONSTRUCT.value:
            retry_input = failed_task.retry_input or {}
            sections = retry_input.get("sections")
            if not isinstance(sections, list):
                raise RavenError(
                    ErrorCode.INVALID_RETRY_INPUT,
                    "Reconstruction retry input does not contain a section list.",
                )
            return await self.reconstructor.reconstruct(
                sections,
                retry_of=failed_task,
            )

        if failed_task.name == OperationType.SESSION_GENERATE_RESPONSE.value:
            self._ensure_models_loaded()
            try:
                retry_input = SessionRetryInput.model_validate(
                    failed_task.retry_input
                )
            except ValidationError as exc:
                raise RavenError(
                    ErrorCode.INVALID_RETRY_INPUT,
                    "The session retry input is invalid.",
                ) from exc

            conversation = self.get_conversation(retry_input.conversation_id)
            session = self.session(
                conversation,
                max_iterations=retry_input.max_iterations,
                top_k=retry_input.top_k,
                memory_token_limit=retry_input.memory_token_limit,
                memory_top_k=retry_input.memory_top_k,
            )
            await session.start()
            run = await session.generate_response(
                retry_input.user_query,
                retrieval_mode=retry_input.retrieval_mode,
                retry_of=failed_task,
            )
            return run.task

        raise RavenError(
            ErrorCode.OPERATION_TASK_NOT_RETRYABLE,
            f"Operation task type '{failed_task.name}' has no retry handler.",
        )


    async def wait_operation(self, operation_id: UUID | str) -> Operation:
        return await self.operation_manager.wait(operation_id)


    async def cancel_operation(self, operation_id: UUID | str) -> Operation:
        return await self.operation_manager.cancel(operation_id)


    async def operation_events(
        self,
        operation_id: UUID | str,
        *,
        after_event_id: int = 0,
    ) -> AsyncIterator[Event]:
        async for event in self.operation_manager.events(
            operation_id,
            after_event_id=after_event_id,
        ):
            yield event


    async def read_operation_events(
        self,
        operation_id: UUID | str,
        *,
        after_event_id: int = 0,
        limit: int | None = None,
    ) -> list[Event]:
        return await self.operation_manager.read_events(
            operation_id,
            after_event_id=after_event_id,
            limit=limit,
        )


    async def check_ollama_connection(
        self,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self.provider.ollama.check_connection(operation=operation)


    async def list_ollama_models(
        self,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self.provider.ollama.list_models(operation=operation)


    async def inspect_ollama_model(
        self,
        model: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self.provider.ollama.inspect(model, operation=operation)


    async def pull_ollama_model(
        self,
        model: str,
        on_progress: ProgressCallback | None = None,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self.provider.ollama.pull(
            model,
            on_progress,
            operation=operation,
        )


    async def delete_ollama_model(
        self,
        model: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self.provider.ollama.delete(model, operation=operation)


    async def unload_ollama_llm(
        self,
        model: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self.provider.ollama.unload_llm(model, operation=operation)


    async def unload_ollama_embedding(
        self,
        model: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self.provider.ollama.unload_embedding(
            model,
            operation=operation,
        )


    async def load(
        self,
        llm_spec: ModelSpec,
        embedding_spec: ModelSpec,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Load both model adapters and configure model-dependent components."""
        self._validate_model_specs(llm_spec, embedding_spec)
        active_operation = operation or await self.operation_manager.create(
            OperationType.RAVEN_LOAD_MODELS
        )
        return await active_operation.run(
            OperationType.RAVEN_LOAD_MODELS,
            lambda active_operation: self._load_models(
                llm_spec,
                embedding_spec,
                operation=active_operation,
            ),
        )


    async def _load_models(
        self,
        llm_spec: ModelSpec,
        embedding_spec: ModelSpec,
        *,
        operation: Operation,
    ) -> dict[str, dict[str, str]]:
        async with self._model_lock:
            with self._session_registry_lock:
                self._models_reloading = True
            try:
                llm_task = await self.provider.load(llm_spec, operation=operation)
                embedding_task = await self.provider.load(
                    embedding_spec,
                    operation=operation,
                )
                loaded = await asyncio.gather(
                    llm_task.result(),
                    embedding_task.result(),
                    return_exceptions=True,
                )
                failures = [
                    value for value in loaded if isinstance(value, BaseException)
                ]
                if failures:
                    raise failures[0]

                llm, embed_model = loaded
                self._validate_model_capabilities(llm, embed_model)
                models_changed = (
                    self._llm_spec != llm_spec
                    or self._embedding_spec != embedding_spec
                )
                if models_changed:
                    await self.knowledge_base.validate_embedding_model(embed_model)
                    with self._session_registry_lock:
                        sessions = tuple(self._sessions.values())
                    for session in sessions:
                        await session.conversation.validate_embedding_model(embed_model)
                    await self._close_sessions()

                self.llm = llm
                self.embed_model = embed_model
                self._llm_spec = llm_spec
                self._embedding_spec = embedding_spec
                self._configure_model_components()
            finally:
                with self._session_registry_lock:
                    self._models_reloading = False
        return {
            "llm": self._model_result(llm_spec),
            "embedding": self._model_result(embedding_spec),
        }


    def _configure_model_components(self) -> None:
        if self.llm is None or self.embed_model is None:
            raise RavenError(
                ErrorCode.LLM_MODEL_REQUIRED,
                "Both LLM and embedding models are required.",
            )

        embedded_retrieval = EmbeddedRetrievalPipeline(
            self.knowledge_base,
            self.embed_model,
            self.operation_manager,
        )
        hierarchical_retrieval = HierarchicalRetrievalPipeline(
            self.knowledge_base,
            self.llm,
            self.operation_manager,
        )
        agreement_retrieval = AgreementBasedRetrievalPipeline(
            self.knowledge_base,
            embedded_retrieval,
            hierarchical_retrieval,
            self.operation_manager,
        )
        vector_conditioned_retrieval = VectorConditionedRetrievalPipeline(
            self.knowledge_base,
            embedded_retrieval,
            hierarchical_retrieval,
            self.operation_manager,
        )
        self.embedded_retrieval = embedded_retrieval
        self.hierarchical_retrieval = hierarchical_retrieval
        self.agreement_retrieval = agreement_retrieval
        self.vector_conditioned_retrieval = vector_conditioned_retrieval
        self.retrieval_pipelines = RetrievalPipelines(
            embedded=embedded_retrieval,
            hierarchical=hierarchical_retrieval,
            agreement=agreement_retrieval,
            vector_conditioned=vector_conditioned_retrieval,
        )


    @staticmethod
    def _validate_model_specs(
        llm_spec: ModelSpec,
        embedding_spec: ModelSpec,
    ) -> None:
        if llm_spec.role != ModelRole.LLM:
            raise RavenError(
                ErrorCode.LLM_MODEL_REQUIRED,
                "llm_spec must describe an LLM model.",
            )
        if embedding_spec.role != ModelRole.EMBEDDING:
            raise RavenError(
                ErrorCode.EMBEDDING_MODEL_REQUIRED,
                "embedding_spec must describe an embedding model.",
            )


    @staticmethod
    def _model_result(spec: ModelSpec) -> dict[str, str]:
        return {
            "provider": spec.provider,
            "model": spec.model,
            "role": spec.role.value,
        }


    @staticmethod
    def _validate_model_capabilities(llm: Any, embed_model: Any) -> None:
        llm_methods = ("achat", "astream_chat", "astructured_predict")
        embedding_methods = (
            "aget_query_embedding",
            "aget_text_embedding_batch",
            "similarity",
        )
        missing_llm = [name for name in llm_methods if not callable(getattr(llm, name, None))]
        missing_embedding = [
            name
            for name in embedding_methods
            if not callable(getattr(embed_model, name, None))
        ]
        if missing_llm or missing_embedding:
            raise RavenError(
                ErrorCode.MODEL_CAPABILITY_MISSING,
                "Loaded models do not provide Raven's required async interfaces.",
                details={
                    "llm_methods": missing_llm,
                    "embedding_methods": missing_embedding,
                },
            )


    async def start(self) -> None:
        """Recover operations and open Raven's persistent registries."""
        async with self._lifecycle_lock:
            if self.is_started:
                return
            if self._closed:
                raise RavenError(
                    ErrorCode.INTERNAL_ERROR,
                    "Raven has been closed and cannot be started again.",
                )

            try:
                loaded_runtime_config = await asyncio.to_thread(
                    RuntimeConfig.load_or_create,
                    self.paths.runtime_settings_path,
                    self.system_config,
                )
                async with self._runtime_config_lock:
                    self._runtime_config = loaded_runtime_config
                await self.operation_manager.recover()
                await self.knowledge_base.start()
                await self.conversation_manager.start()
            except BaseException:
                self._closed = True
                self._started = False
                await asyncio.gather(
                    self.conversation_manager.close(),
                    self.knowledge_base.close(),
                    self.provider.close(),
                    self.operation_manager.close(),
                    return_exceptions=True,
                )
                raise
            self._started = True


    async def close(self) -> None:
        """Attempt every owned cleanup step and share one shutdown completion."""
        async with self._lifecycle_lock:
            if self._close_task is None:
                self._closed = True
                self._started = False
                with self._session_registry_lock:
                    sessions = list(
                        dict.fromkeys(
                            [
                                *self._sessions.values(),
                                *self._retired_sessions.values(),
                            ]
                        )
                    )
                    self._sessions.clear()
                    self._stale_sessions.clear()
                    self._retired_sessions.clear()
                self._close_task = asyncio.create_task(
                    self._close_owned_resources(sessions)
                )
            close_task = self._close_task

        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            raise
        except BaseException:
            async with self._lifecycle_lock:
                if self._close_task is close_task and close_task.done():
                    self._close_task = None
            raise


    async def _close_owned_resources(self, sessions: list[Session]) -> None:
        failures: list[tuple[str, BaseException]] = []

        async def attempt(name: str, awaitable: Any) -> None:
            try:
                await awaitable
            except BaseException as exc:
                failures.append((name, exc))

        session_results = await asyncio.gather(
            *(session.close() for session in sessions),
            return_exceptions=True,
        )
        failures.extend(
            ("session", result)
            for result in session_results
            if isinstance(result, BaseException)
        )
        maintenance_results = await asyncio.gather(
            *tuple(self._session_maintenance_tasks),
            return_exceptions=True,
        )
        failures.extend(
            ("session maintenance", result)
            for result in maintenance_results
            if isinstance(result, BaseException)
        )
        await attempt("active operations", self.operation_manager.cancel_active())
        if self.knowledge_base.is_started:
            await attempt(
                "knowledge reconciliation",
                self.knowledge_base.reconcile_pending_ingestions(),
            )
            await attempt(
                "knowledge deletion reconciliation",
                self.knowledge_base.reconcile_pending_file_deletions(),
            )
        await attempt("conversations", self.conversation_manager.close())
        await attempt("knowledge base", self.knowledge_base.close())
        await attempt("providers", self.provider.close())
        await attempt("operation manager", self.operation_manager.close())

        if failures:
            raise RavenError(
                ErrorCode.INTERNAL_ERROR,
                "Raven shutdown completed with cleanup failures.",
                details={
                    "failures": [
                        {"resource": name, "error_type": type(error).__name__}
                        for name, error in failures
                    ]
                },
            ) from failures[0][1]


    async def _close_sessions(self) -> None:
        with self._session_registry_lock:
            sessions = list(
                dict.fromkeys(
                    [*self._sessions.values(), *self._retired_sessions.values()]
                )
            )
            self._sessions.clear()
            self._stale_sessions.clear()
            self._retired_sessions.clear()
        results = await asyncio.gather(
            *(session.close() for session in sessions),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise RavenError(
                ErrorCode.INTERNAL_ERROR,
                "One or more sessions could not be closed.",
                details={"failure_count": len(failures)},
            ) from failures[0]


    async def create_knowledge(
        self,
        name: str,
        user_summary: str = "",
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self.knowledge_base.create(
            name,
            user_summary,
            operation=operation,
        )


    def get_knowledge(self, name: str) -> Knowledge:
        return self.knowledge_base.get(name)


    def get_knowledge_details(self, name: str) -> dict[str, Any]:
        return self.knowledge_base.get(name).meta


    async def list_knowledges(self) -> list[dict[str, Any]]:
        return await self.knowledge_base.list()


    async def list_knowledges_page(
        self,
        *,
        limit: int,
        after_name: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.knowledge_base.list_page(limit, after_name)


    async def update_knowledge(
        self,
        name: str,
        *,
        user_summary: str,
        operation: Operation | None = None,
    ) -> OperationTask:
        knowledge = self.knowledge_base.get(name)
        return await knowledge.set_summary(user_summary, operation=operation)


    async def list_knowledge_files(
        self,
        knowledge_name: str,
    ) -> list[dict[str, Any]]:
        knowledge = self.knowledge_base.get(knowledge_name)
        return await asyncio.to_thread(knowledge.list_files)


    async def list_knowledge_files_page(
        self,
        knowledge_name: str,
        *,
        limit: int,
        after_file_id: str | None = None,
    ) -> list[dict[str, Any]]:
        knowledge = self.knowledge_base.get(knowledge_name)
        return await asyncio.to_thread(
            knowledge.list_files_page,
            limit,
            after_file_id,
        )


    async def list_file_sections(
        self,
        knowledge_name: str,
        file_name: str,
    ) -> list[dict[str, Any]]:
        knowledge = self.knowledge_base.get(knowledge_name)
        sections = await asyncio.to_thread(knowledge.list_sections, file_name)
        if not sections and not await asyncio.to_thread(
            knowledge.file_exists,
            file_name,
        ):
            raise RavenError(
                ErrorCode.FILE_NOT_FOUND,
                f"File '{file_name}' does not exist in knowledge '{knowledge_name}'.",
            )
        return sections


    async def list_file_sections_page(
        self,
        knowledge_name: str,
        file_name: str,
        *,
        limit: int,
        after_section_index: int = 0,
    ) -> list[dict[str, Any]]:
        knowledge = self.knowledge_base.get(knowledge_name)
        sections = await asyncio.to_thread(
            knowledge.list_sections_page,
            file_name,
            limit,
            after_section_index,
        )
        if not sections and not await asyncio.to_thread(
            knowledge.file_exists,
            file_name,
        ):
            raise RavenError(
                ErrorCode.FILE_NOT_FOUND,
                f"File '{file_name}' does not exist in knowledge '{knowledge_name}'.",
            )
        return sections


    async def get_knowledge_section(
        self,
        knowledge_name: str,
        section_id: str,
        *,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        knowledge = self.knowledge_base.get(knowledge_name)
        section = await asyncio.to_thread(knowledge.get_section, section_id)
        if section is None or (
            file_name is not None and section.get("file_name") != file_name
        ):
            raise RavenError(
                ErrorCode.SECTION_NOT_FOUND,
                f"Section '{section_id}' does not exist in knowledge '{knowledge_name}'.",
            )
        return section


    async def count_knowledge_vectors(self, knowledge_name: str) -> int:
        knowledge = self.knowledge_base.get(knowledge_name)
        return await knowledge.count()


    async def count_knowledge_files(self, knowledge_name: str) -> int:
        knowledge = self.knowledge_base.get(knowledge_name)
        return await asyncio.to_thread(knowledge.file_count)


    async def delete_knowledge_file(
        self,
        knowledge_name: str,
        file_id: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        knowledge = self.knowledge_base.get(knowledge_name)
        return await knowledge.delete_file(file_id, operation=operation)


    async def delete_knowledge(
        self,
        name: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self.knowledge_base.delete(name, operation=operation)


    async def ingest(
        self,
        knowledge_name: str,
        source_path: str | Path,
        *,
        breakpoint_percentile_threshold: int | None = None,
        buffer_size: int | None = None,
        max_extraction_retries: int | None = None,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        max_source_size_bytes: int | None = None,
        max_document_pages: int | None = None,
        operation: Operation | None = None,
    ) -> OperationTask:
        self._ensure_models_loaded()
        assert self.llm is not None
        assert self.embed_model is not None
        settings = self._runtime_config
        effective_breakpoint = (
            settings.breakpoint_percentile_threshold
            if breakpoint_percentile_threshold is None
            else breakpoint_percentile_threshold
        )
        effective_buffer_size = (
            settings.buffer_size if buffer_size is None else buffer_size
        )
        effective_retries = (
            settings.max_extraction_retries
            if max_extraction_retries is None
            else max_extraction_retries
        )
        effective_chunk_size = (
            settings.chunk_size if chunk_size is None else chunk_size
        )
        effective_chunk_overlap = (
            settings.chunk_overlap if chunk_overlap is None else chunk_overlap
        )
        effective_source_limit = (
            self.system_config.max_source_file_bytes
            if max_source_size_bytes is None
            else max_source_size_bytes
        )
        effective_page_limit = (
            self.system_config.max_document_pages
            if max_document_pages is None
            else max_document_pages
        )
        self._validate_ingestion_overrides(
            breakpoint_percentile_threshold=effective_breakpoint,
            buffer_size=effective_buffer_size,
            max_extraction_retries=effective_retries,
            chunk_size=effective_chunk_size,
            chunk_overlap=effective_chunk_overlap,
            max_source_size_bytes=effective_source_limit,
            max_document_pages=effective_page_limit,
        )
        pipeline = IngestionPipeline(
            self.knowledge_base,
            self.operation_manager,
            breakpoint_percentile_threshold=effective_breakpoint,
            buffer_size=effective_buffer_size,
            max_extraction_retries=effective_retries,
            document_parser=self._document_parser,
        )
        return await pipeline.run(
            knowledge_name,
            source_path,
            llm=self.llm,
            embed_model=self.embed_model,
            chunk_size=effective_chunk_size,
            chunk_overlap=effective_chunk_overlap,
            max_source_size_bytes=effective_source_limit,
            max_document_pages=effective_page_limit,
            operation=operation,
        )


    async def retrieve(
        self,
        mode: RetrievalMode | str,
        user_query: str,
        *,
        knowledge_name: str | None = None,
        top_k: int | None = None,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Run one retrieval strategy while preserving its operation events."""
        self._ensure_models_loaded()
        try:
            selected = mode if isinstance(mode, RetrievalMode) else RetrievalMode(mode)
        except ValueError as exc:
            raise RavenError(
                ErrorCode.INVALID_RETRIEVAL_MODE,
                f"Unknown retrieval mode '{mode}'.",
            ) from exc

        effective_top_k = self._runtime_config.retrieval_top_k if top_k is None else top_k
        self._validate_retrieval_top_k(effective_top_k)

        if selected in LOCAL_RETRIEVAL_MODES:
            if not knowledge_name:
                raise RavenError(
                    ErrorCode.INVALID_METADATA,
                    "knowledge_name is required for local retrieval.",
                )
            method_name = selected.value.removeprefix("local_")
            pipeline = getattr(self, f"{method_name}_retrieval")
            return await pipeline.retrieve_local_context(
                knowledge_name,
                user_query,
                effective_top_k,
                operation=operation,
            )

        if selected in GLOBAL_RETRIEVAL_MODES:
            method_name = selected.value.removeprefix("global_")
            pipeline = getattr(self, f"{method_name}_retrieval")
            return await pipeline.retrieve_global_context(
                user_query,
                effective_top_k,
                operation=operation,
            )

        raise RavenError(
            ErrorCode.INVALID_RETRIEVAL_MODE,
            f"Unsupported retrieval mode '{selected.value}'.",
        )


    async def reconstruct(
        self,
        retrieval_result: list[dict[str, Any]] | dict[str, Any] | None,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self.reconstructor.reconstruct(
            retrieval_result,
            operation=operation,
        )


    async def reconstruct_from_turn(
        self,
        conversation_id: str,
        turn_id: UUID | str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Reconstruct sources from evidence persisted in one conversation turn."""
        active_operation = operation or await self.operation_manager.create(
            OperationType.RECONSTRUCTION_FROM_TURN
        )
        return await active_operation.run(
            OperationType.RECONSTRUCTION_FROM_TURN,
            lambda active_operation: self._reconstruct_from_turn(
                conversation_id,
                turn_id,
                operation=active_operation,
            ),
        )


    async def _reconstruct_from_turn(
        self,
        conversation_id: str,
        turn_id: UUID | str,
        *,
        operation: Operation,
    ) -> list[dict[str, Any]]:
        conversation = self.get_conversation(conversation_id)
        messages = await conversation.get_turn_messages(turn_id)
        evidence = self._reconstruction_evidence(messages)
        reconstruction_task = await self.reconstructor.reconstruct(
            evidence,
            operation=operation,
        )
        return await reconstruction_task.result()


    @staticmethod
    def _reconstruction_evidence(
        messages: list[ChatMessage],
    ) -> list[dict[str, str]]:
        evidence: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()

        for message in messages:
            if message.role != MessageRole.TOOL or not isinstance(message.content, str):
                continue
            try:
                payload = json.loads(message.content)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                continue
            references = payload.get("evidence")
            if not isinstance(references, list):
                continue

            for reference in references:
                if not isinstance(reference, dict):
                    continue
                knowledge_name = reference.get("knowledge_name")
                file_name = reference.get("file_name")
                section_id = reference.get("section_id")
                if not isinstance(knowledge_name, str) or not knowledge_name:
                    continue
                if not isinstance(file_name, str) or not file_name:
                    continue
                if not isinstance(section_id, str) or not section_id:
                    continue
                key = (knowledge_name, file_name, section_id)
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(
                    {
                        "knowledge_name": knowledge_name,
                        "file_name": file_name,
                        "section_id": section_id,
                    }
                )

        return evidence


    async def create_conversation(
        self,
        knowledge_name: str | None = None,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self.conversation_manager.create(
            knowledge_name,
            operation=operation,
        )


    def get_conversation(self, conversation_id: str) -> Conversation:
        return self.conversation_manager.get(conversation_id)


    def get_conversation_details(self, conversation_id: str) -> dict[str, Any]:
        return self.conversation_manager.get(conversation_id).to_dict()


    def list_conversations(self) -> list[dict[str, Any]]:
        return self.conversation_manager.list()


    def list_conversations_page(
        self,
        *,
        limit: int,
        after_conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.conversation_manager.list_page(limit, after_conversation_id)


    async def get_conversation_messages(
        self,
        conversation_id: str,
    ) -> list[ChatMessage]:
        conversation = self.conversation_manager.get(conversation_id)
        return await conversation.get_messages()


    async def get_conversation_messages_page(
        self,
        conversation_id: str,
        *,
        limit: int,
        after_message_id: int = 0,
    ) -> list[dict[str, Any]]:
        conversation = self.conversation_manager.get(conversation_id)
        return await conversation.get_messages_page(limit, after_message_id)


    async def get_conversation_turn(
        self,
        conversation_id: str,
        turn_id: UUID | str,
    ) -> dict[str, Any]:
        conversation = self.conversation_manager.get(conversation_id)
        turn = await conversation.get_turn(turn_id)
        if turn is None:
            raise RavenError(
                ErrorCode.CONVERSATION_TURN_NOT_FOUND,
                f"Turn '{turn_id}' does not exist in conversation '{conversation_id}'.",
            )
        return turn


    async def get_conversation_turn_messages(
        self,
        conversation_id: str,
        turn_id: UUID | str,
    ) -> list[ChatMessage]:
        conversation = self.conversation_manager.get(conversation_id)
        return await conversation.get_turn_messages(turn_id)


    async def get_conversation_turn_messages_page(
        self,
        conversation_id: str,
        turn_id: UUID | str,
        *,
        limit: int,
        after_message_id: int = 0,
    ) -> list[dict[str, Any]]:
        conversation = self.conversation_manager.get(conversation_id)
        return await conversation.get_turn_messages_page(
            turn_id,
            limit,
            after_message_id,
        )


    async def get_conversation_preferences(
        self,
        conversation_id: str,
    ) -> list[dict[str, str]]:
        conversation = self.conversation_manager.get(conversation_id)
        return await conversation.list_preferences()


    async def save_conversation_preference(
        self,
        conversation_id: str,
        text: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        conversation = self.conversation_manager.get(conversation_id)
        return await conversation.save_preference(text, operation=operation)


    async def remove_conversation_preference(
        self,
        conversation_id: str,
        preference_id: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        conversation = self.conversation_manager.get(conversation_id)
        return await conversation.remove_preference(
            preference_id,
            operation=operation,
        )


    def list_discovery_issues(self) -> list[DiscoveryIssue]:
        """Return persisted knowledge and conversation resources that failed validation."""
        return [
            *self.knowledge_base.list_discovery_issues(),
            *self.conversation_manager.list_discovery_issues(),
        ]


    async def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        operation: Operation | None = None,
    ) -> OperationTask:
        return await self.conversation_manager.update(
            conversation_id,
            title=title,
            pinned=pinned,
            operation=operation,
        )


    async def delete_conversation(
        self,
        conversation_id: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        with self._session_registry_lock:
            session = self._sessions.pop(conversation_id, None)
            self._stale_sessions.discard(conversation_id)
        if session is not None:
            await session.close()
        return await self.conversation_manager.delete(
            conversation_id,
            operation=operation,
        )


    def session(
        self,
        conversation: Conversation,
        *,
        max_iterations: int | None = None,
        top_k: int | None = None,
        memory_token_limit: int | None = None,
        memory_top_k: int | None = None,
    ) -> Session:
        """Return the managed session for a conversation object."""
        self._ensure_models_loaded()
        assert self.llm is not None
        assert self.embed_model is not None
        assert self.retrieval_pipelines is not None
        settings = self._runtime_config
        effective_max_iterations = (
            settings.agent_max_iterations
            if max_iterations is None
            else max_iterations
        )
        effective_top_k = settings.retrieval_top_k if top_k is None else top_k
        effective_memory_token_limit = (
            settings.memory_token_limit
            if memory_token_limit is None
            else memory_token_limit
        )
        effective_memory_top_k = (
            settings.memory_top_k if memory_top_k is None else memory_top_k
        )
        self._validate_session_overrides(
            max_iterations=effective_max_iterations,
            top_k=effective_top_k,
            memory_token_limit=effective_memory_token_limit,
            memory_top_k=effective_memory_top_k,
        )
        if not self.conversation_manager.owns(conversation):
            raise RavenError(
                ErrorCode.FOREIGN_CONVERSATION,
                "The supplied conversation does not belong to this Raven instance.",
            )
        conversation_id = conversation.conversation_id
        with self._session_registry_lock:
            if self._models_reloading:
                raise RavenError(
                    ErrorCode.MODEL_RELOAD_IN_PROGRESS,
                    "A model reload is in progress; create the session after it completes.",
                )
            existing = self._sessions.get(conversation_id)
            if existing is not None:
                if existing.is_closed:
                    self._sessions.pop(conversation_id, None)
                elif existing.matches_configuration(
                    llm=self.llm,
                    embed_model=self.embed_model,
                    max_iterations=effective_max_iterations,
                    top_k=effective_top_k,
                    memory_token_limit=effective_memory_token_limit,
                    memory_top_k=effective_memory_top_k,
                ):
                    return existing
                else:
                    raise RavenError(
                        ErrorCode.CONVERSATION_SESSION_CONFIGURATION_CONFLICT,
                        "The active session uses different runtime settings.",
                        details={"conversation_id": conversation_id},
                    )

            harness = AgentHarness(
                llm=self.llm,
                knowledge_base=self.knowledge_base,
                retrieval_pipelines=self.retrieval_pipelines,
                operation_manager=self.operation_manager,
                reconstructor=self.reconstructor,
                max_iterations=effective_max_iterations,
                top_k=effective_top_k,
                max_cached_prompts=self.system_config.prompt_cache_size,
            )
            session = Session(
                conversation,
                harness,
                self.operation_manager,
                llm=self.llm,
                embed_model=self.embed_model,
                memory_token_limit=effective_memory_token_limit,
                memory_top_k=effective_memory_top_k,
                on_idle=self._session_became_idle,
            )
            self._sessions[conversation_id] = session
            return session


    def _validate_ingestion_overrides(
        self,
        *,
        breakpoint_percentile_threshold: int,
        buffer_size: int,
        max_extraction_retries: int,
        chunk_size: int,
        chunk_overlap: int,
        max_source_size_bytes: int,
        max_document_pages: int,
    ) -> None:
        current = self._runtime_config
        RuntimeConfig(
            revision=current.revision,
            breakpoint_percentile_threshold=breakpoint_percentile_threshold,
            buffer_size=buffer_size,
            max_extraction_retries=max_extraction_retries,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            retrieval_top_k=current.retrieval_top_k,
            agent_max_iterations=current.agent_max_iterations,
            memory_token_limit=current.memory_token_limit,
            memory_top_k=current.memory_top_k,
        ).validate_against(self.system_config)
        self._validate_system_bounded_int(
            max_source_size_bytes,
            "max_source_size_bytes",
            self.system_config.max_source_file_bytes,
        )
        self._validate_system_bounded_int(
            max_document_pages,
            "max_document_pages",
            self.system_config.max_document_pages,
        )


    def _validate_retrieval_top_k(self, top_k: int) -> None:
        self._validate_system_bounded_int(
            top_k,
            "top_k",
            self.system_config.max_retrieval_top_k,
        )


    def _validate_session_overrides(
        self,
        *,
        max_iterations: int,
        top_k: int,
        memory_token_limit: int,
        memory_top_k: int,
    ) -> None:
        self._validate_retrieval_top_k(top_k)
        self._validate_system_bounded_int(
            max_iterations,
            "max_iterations",
            self.system_config.max_agent_iterations,
        )
        for field_name, value in (
            ("memory_token_limit", memory_token_limit),
            ("memory_top_k", memory_top_k),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RavenError(
                    ErrorCode.INVALID_RUNTIME_CONFIG,
                    f"{field_name} must be a positive integer.",
                    details={"field": field_name},
                )


    @staticmethod
    def _validate_system_bounded_int(
        value: int,
        field_name: str,
        maximum: int,
    ) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RavenError(
                ErrorCode.INVALID_RUNTIME_CONFIG,
                f"{field_name} must be a positive integer.",
                details={"field": field_name},
            )
        if value > maximum:
            raise RavenError(
                ErrorCode.INVALID_RUNTIME_CONFIG,
                f"{field_name} cannot exceed the system maximum of {maximum}.",
                details={"field": field_name, "maximum": maximum},
            )


    def _ensure_models_loaded(self) -> None:
        if self.llm is None:
            raise RavenError(
                ErrorCode.LLM_MODEL_REQUIRED,
                "Call Raven.load() before using model-dependent operations.",
            )
        if self.embed_model is None:
            raise RavenError(
                ErrorCode.EMBEDDING_MODEL_REQUIRED,
                "Call Raven.load() before using model-dependent operations.",
            )
        if self.retrieval_pipelines is None:
            raise RavenError(
                ErrorCode.INTERNAL_ERROR,
                "Model-dependent Raven components are not configured.",
            )
