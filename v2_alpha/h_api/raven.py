"""High-level library facade for Raven."""

from __future__ import annotations

import asyncio
from typing import Any

from ..agent.contracts import RetrievalPipelines
from ..agent.harness import AgentHarness
from ..core.config import PathConfig
from ..core.events import EventStreamRegistry
from ..core.operations import OperationManager
from ..data_management.conversation_manager import Conversation, ConversationManager
from ..data_management.knowledge_base import KnowledgeBase
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
    """Own and connect Raven's library components."""

    def __init__(
        self,
        paths: PathConfig,
        *,
        llm: Any,
        embed_model: Any,
        max_agent_iterations: int = 10,
        agent_top_k: int = 3,
    ) -> None:
        if llm is None:
            raise ValueError("llm is required")
        if embed_model is None:
            raise ValueError("embed_model is required")

        self.paths = paths
        self.llm = llm
        self.embed_model = embed_model

        self._event_streams = EventStreamRegistry(paths)
        self._operation_manager = OperationManager(self._event_streams)
        self._knowledge_base = KnowledgeBase(paths, self._operation_manager)
        self._conversation_manager = ConversationManager(
            paths,
            self._knowledge_base,
            self._operation_manager,
        )

        self._ingestion_pipeline = IngestionPipeline(
            self._knowledge_base,
            self._operation_manager,
        )
        embedded_retrieval = EmbeddedRetrievalPipeline(
            self._knowledge_base,
            embed_model,
            self._operation_manager,
        )
        hierarchical_retrieval = HierarchicalRetrievalPipeline(
            self._knowledge_base,
            llm,
            self._operation_manager,
        )
        agreement_retrieval = AgreementBasedRetrievalPipeline(
            self._knowledge_base,
            embedded_retrieval,
            hierarchical_retrieval,
            self._operation_manager,
        )
        vector_conditioned_retrieval = VectorConditionedRetrievalPipeline(
            self._knowledge_base,
            embedded_retrieval,
            hierarchical_retrieval,
            self._operation_manager,
        )
        retrieval_pipelines = RetrievalPipelines(
            embedded=embedded_retrieval,
            hierarchical=hierarchical_retrieval,
            agreement=agreement_retrieval,
            vector_conditioned=vector_conditioned_retrieval,
        )
        self._reconstructor = Reconstructor(
            self._knowledge_base,
            self._operation_manager,
        )
        self._harness = AgentHarness(
            llm,
            self._knowledge_base,
            retrieval_pipelines,
            self._operation_manager,
            reconstructor=self._reconstructor,
            max_iterations=max_agent_iterations,
            top_k=agent_top_k,
        )

        self._lifecycle_lock = asyncio.Lock()
        self._sessions: dict[str, Session] = {}
        self._started = False
        self._closed = False


    @property
    def is_started(self) -> bool:
        return self._started and not self._closed


    async def start(self) -> None:
        """Start Raven's persistent managers."""
        async with self._lifecycle_lock:
            if self.is_started:
                return
            if self._closed:
                raise RuntimeError("Raven is closed and cannot be restarted")

            await self._knowledge_base.start()
            try:
                await self._conversation_manager.start()
            except Exception:
                await self._knowledge_base.close()
                raise
            self._started = True


    async def close(self) -> None:
        """Close sessions, operations, managers, and persistent resources."""
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._started = False

        await asyncio.gather(
            *(session.close() for session in self._sessions.values()),
            return_exceptions=True,
        )
        self._sessions.clear()
        await self._operation_manager.close()
        await self._conversation_manager.close()
        await self._knowledge_base.close()


    def session(self, conversation: Conversation) -> Session:
        """Create a session for a complete conversation object."""
        self._ensure_started()
        managed = self._conversation_manager.get(conversation.conversation_id)
        if managed is not conversation:
            raise ValueError(
                "conversation must be managed by this Raven instance"
            )

        existing = self._sessions.get(conversation.conversation_id)
        if existing is not None:
            return existing

        session = Session(
            conversation,
            self._harness,
            self._operation_manager,
            llm=self.llm,
            embed_model=self.embed_model,
        )
        self._sessions[conversation.conversation_id] = session
        return session


    def _ensure_started(self) -> None:
        if self._closed:
            raise RuntimeError("Raven is closed")
        if not self._started:
            raise RuntimeError("Raven has not been started")
