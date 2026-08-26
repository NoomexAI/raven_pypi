"""High-level library API for Raven."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

from ..agent.contracts import RetrievalPipelines
from ..agent.harness import AgentHarness
from ..agent.policy import GLOBAL_RETRIEVAL_MODES, LOCAL_RETRIEVAL_MODES, RetrievalMode
from ..core.config import PathConfig
from ..core.errors import ErrorCode, RavenError
from ..core.events import Event, EventStreamRegistry
from ..core.operations import (
    Operation,
    OperationManager,
    OperationTask,
    OperationWorker,
    TaskWorker,
)
from ..data_management.conversation_manager import Conversation, ConversationManager
from ..data_management.knowledge_base import Knowledge, KnowledgeBase
from ..pipelines.ingestion import IngestionPipeline
from ..pipelines.reconstructor import Reconstructor
from ..pipelines.retrieval import (
    AgreementBasedRetrievalPipeline,
    EmbeddedRetrievalPipeline,
    HierarchicalRetrievalPipeline,
    VectorConditionedRetrievalPipeline,
)
from ..session.session import Session


class Raven:
    """Compose Raven's components behind one operation-first API."""

    def __init__(
        self,
        raven_home: str | Path | PathConfig,
        *,
        llm: Any,
        embed_model: Any,
    ) -> None:
        if llm is None:
            raise ValueError("llm is required")
        if embed_model is None:
            raise ValueError("embed_model is required")

        self.paths = (
            raven_home
            if isinstance(raven_home, PathConfig)
            else PathConfig(Path(raven_home))
        )
        self.llm = llm
        self.embed_model = embed_model

        self.event_streams = EventStreamRegistry(self.paths)
        self.operation_manager = OperationManager(self.event_streams)
        self.knowledge_base = KnowledgeBase(self.paths, self.operation_manager)
        self.conversation_manager = ConversationManager(
            self.paths,
            self.knowledge_base,
            self.operation_manager,
        )
        self.embedded_retrieval = EmbeddedRetrievalPipeline(
            self.knowledge_base,
            self.embed_model,
            self.operation_manager,
        )
        self.hierarchical_retrieval = HierarchicalRetrievalPipeline(
            self.knowledge_base,
            self.llm,
            self.operation_manager,
        )
        self.agreement_retrieval = AgreementBasedRetrievalPipeline(
            self.knowledge_base,
            self.embedded_retrieval,
            self.hierarchical_retrieval,
            self.operation_manager,
        )
        self.vector_conditioned_retrieval = VectorConditionedRetrievalPipeline(
            self.knowledge_base,
            self.embedded_retrieval,
            self.hierarchical_retrieval,
            self.operation_manager,
        )
        self.reconstructor = Reconstructor(
            self.knowledge_base,
            self.operation_manager,
        )
        self.retrieval_pipelines = RetrievalPipelines(
            embedded=self.embedded_retrieval,
            hierarchical=self.hierarchical_retrieval,
            agreement=self.agreement_retrieval,
            vector_conditioned=self.vector_conditioned_retrieval,
        )
        self._sessions: dict[str, Session] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._closed = False


    @property
    def is_started(self) -> bool:
        return self._started and not self._closed


    async def submit_operation(
        self,
        name: str,
        worker: OperationWorker,
    ) -> Operation:
        """Create and start a root operation from a worker."""
        return await self.operation_manager.submit(name, worker)


    async def run_operation(
        self,
        name: str,
        worker: TaskWorker,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Run a root task or a child task in a supplied operation."""
        return await self.operation_manager.run(
            name,
            worker,
            operation=operation,
        )


    async def get_operation(self, operation_id: UUID | str) -> Operation:
        return await self.operation_manager.get(operation_id)


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


    async def start(self) -> None:
        """Open Raven's knowledge and conversation registries."""
        async with self._lifecycle_lock:
            if self.is_started:
                return
            if self._closed:
                raise RavenError(
                    ErrorCode.INTERNAL_ERROR,
                    "Raven has been closed and cannot be started again.",
                )

            await self.knowledge_base.start()
            try:
                await self.conversation_manager.start()
            except Exception:
                await self.knowledge_base.close()
                raise
            self._started = True


    async def close(self) -> None:
        """Close sessions, storage registries, and the operation manager."""
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._started = False
            sessions = list(self._sessions.values())
            self._sessions.clear()

        await asyncio.gather(*(session.close() for session in sessions))
        await self.conversation_manager.close()
        await self.knowledge_base.close()
        await self.operation_manager.close()


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


    async def list_knowledges(self) -> list[dict[str, Any]]:
        return await self.knowledge_base.list()


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
        breakpoint_percentile_threshold: int = 95,
        buffer_size: int = 1,
        max_extraction_retries: int = 3,
        chunk_size: int = 512,
        chunk_overlap: int = 50,
        operation: Operation | None = None,
    ) -> OperationTask:
        pipeline = IngestionPipeline(
            self.knowledge_base,
            self.operation_manager,
            breakpoint_percentile_threshold=breakpoint_percentile_threshold,
            buffer_size=buffer_size,
            max_extraction_retries=max_extraction_retries,
        )
        return await pipeline.run(
            knowledge_name,
            source_path,
            llm=self.llm,
            embed_model=self.embed_model,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            operation=operation,
        )


    async def retrieve(
        self,
        mode: RetrievalMode | str,
        user_query: str,
        *,
        knowledge_name: str | None = None,
        top_k: int = 3,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Run one retrieval strategy while preserving its operation events."""
        try:
            selected = mode if isinstance(mode, RetrievalMode) else RetrievalMode(mode)
        except ValueError as exc:
            raise RavenError(
                ErrorCode.INVALID_RETRIEVAL_MODE,
                f"Unknown retrieval mode '{mode}'.",
            ) from exc

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
                top_k,
                operation=operation,
            )

        if selected in GLOBAL_RETRIEVAL_MODES:
            method_name = selected.value.removeprefix("global_")
            pipeline = getattr(self, f"{method_name}_retrieval")
            return await pipeline.retrieve_global_context(
                user_query,
                top_k,
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


    def list_conversations(self) -> list[dict[str, Any]]:
        return self.conversation_manager.list()


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
        return await self.conversation_manager.delete(
            conversation_id,
            operation=operation,
        )


    def session(
        self,
        conversation: Conversation,
        *,
        max_iterations: int = 10,
        top_k: int = 3,
        memory_token_limit: int = 4000,
        memory_top_k: int = 5,
    ) -> Session:
        """Return the managed session for a conversation object."""
        conversation_id = conversation.conversation_id
        existing = self._sessions.get(conversation_id)
        if existing is not None:
            return existing

        harness = AgentHarness(
            llm=self.llm,
            knowledge_base=self.knowledge_base,
            retrieval_pipelines=self.retrieval_pipelines,
            operation_manager=self.operation_manager,
            reconstructor=self.reconstructor,
            max_iterations=max_iterations,
            top_k=top_k,
        )
        session = Session(
            conversation,
            harness,
            self.operation_manager,
            llm=self.llm,
            embed_model=self.embed_model,
            memory_token_limit=memory_token_limit,
            memory_top_k=memory_top_k,
        )
        self._sessions[conversation_id] = session
        return session
