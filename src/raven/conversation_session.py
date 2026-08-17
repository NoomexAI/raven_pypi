"""Conversation-scoped state and resources.

This module intentionally does not construct agents, prompts, or tools.  The
agent package is the owner of model behavior; this class only owns the state
that must survive across turns for one conversation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, cast

from llama_index.core.llms import ChatMessage
from llama_index.core.memory import ChatSummaryMemoryBuffer, SimpleComposableMemory, VectorMemory
from llama_index.core.memory.memory_blocks import FactExtractionMemoryBlock
from llama_index.core.storage.chat_store import SimpleChatStore
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.llms.ollama import Ollama
from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models

from .conversation_manager import Conversation
from .events import EventBus
from .knowledge import KnowledgeBase
from .pipeline import (
    GLOBAL_AGREEMENT_RETRIEVAL,
    GLOBAL_EMBEDDED_RETRIEVAL,
    GLOBAL_HIERARCHICAL_RETRIEVAL,
    GLOBAL_VECTOR_CONDITIONED_RETRIEVAL,
    LOCAL_AGREEMENT_RETRIEVAL,
    LOCAL_EMBEDDED_RETRIEVAL,
    LOCAL_HIERARCHICAL_RETRIEVAL,
    LOCAL_VECTOR_CONDITIONED_RETRIEVAL,
    AgreementBasedRetrievalPipeline,
    EmbeddedRetrievalPipeline,
    HierarchicalRetrievalPipeline,
    VectorConditionedRetrievalPipeline,
)
from .reconstructor import Reconstructor

logger = logging.getLogger(__name__)


class ConversationSession:
    """Stateful resources for one conversation, with no agent behavior."""

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        bus: EventBus,
        conversation: Conversation,
        llm: Ollama,
        embed_model: OllamaEmbedding,
        embedded_pipeline: EmbeddedRetrievalPipeline,
        hierarchical_pipeline: HierarchicalRetrievalPipeline,
        agreement_pipeline: AgreementBasedRetrievalPipeline,
        vector_conditioned_pipeline: VectorConditionedRetrievalPipeline,
        reconstructor: Reconstructor,
        token_limit: int | None = None,
    ) -> None:
        self._kb = knowledge_base
        self._bus = bus
        self.conversation = conversation
        self._llm = llm
        self._embed_model = embed_model
        self._embedded = embedded_pipeline
        self._hierarchical = hierarchical_pipeline
        self._agreement = agreement_pipeline
        self._vector_conditioned = vector_conditioned_pipeline
        self._reconstructor = reconstructor
        self._token_limit = token_limit

        self._facts_block = FactExtractionMemoryBlock(llm=llm)
        self._facts_signature = 0
        self._load_facts()
        self._memory = self._build_memory()
        self._messages_seen = len(
            cast(ChatSummaryMemoryBuffer, self._memory.primary_memory)
            .chat_store.get_messages("messages")
        )
        self.title_generated = conversation.title != "New Conversation"
        self._closed = False

    @classmethod
    async def create(cls, **kwargs: Any) -> "ConversationSession":
        return await asyncio.to_thread(cls, **kwargs)

    @property
    def memory(self) -> SimpleComposableMemory:
        return self._memory

    @property
    def llm(self) -> Ollama:
        return self._llm

    @property
    def facts(self) -> list[str]:
        return self._facts_block.facts

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._qdrant_client.close()
        except Exception:
            pass

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    def _load_facts(self) -> None:
        facts_path = self.conversation.dir_path / "facts.json"
        if facts_path.exists():
            try:
                data = json.loads(facts_path.read_text(encoding="utf-8"))
                self._facts_block.facts = list(data.get("facts", []))
                self._facts_signature = len(self._facts_block.facts)
            except Exception:
                logger.warning("failed to load conversation facts", exc_info=True)

    def _build_memory(self) -> SimpleComposableMemory:
        chat_store = SimpleChatStore()
        if self.conversation.messages_path.exists():
            chat_store = SimpleChatStore.from_persist_path(str(self.conversation.messages_path))
        summary = ChatSummaryMemoryBuffer.from_defaults(
            llm=self._llm,
            chat_store=chat_store,
            chat_store_key="messages",
            token_limit=self._token_limit,
        )

        self.conversation.qdrant_dir.mkdir(parents=True, exist_ok=True)
        qdrant_client = QdrantClient(path=str(self.conversation.qdrant_dir))
        self._qdrant_client = qdrant_client
        vector_name = "text-dense"
        if not qdrant_client.collection_exists("messages"):
            probe = self._embed_model.get_text_embedding("raven memory probe")
            qdrant_client.create_collection(
                collection_name="messages",
                vectors_config={vector_name: qdrant_models.VectorParams(
                    size=len(probe), distance=qdrant_models.Distance.COSINE
                )},
            )
        vector_store = QdrantVectorStore(
            collection_name="messages",
            client=qdrant_client,
            dense_vector_name=vector_name,
        )
        self._vector_memory = VectorMemory.from_defaults(
            vector_store=vector_store,
            embed_model=self._embed_model,
            retriever_kwargs={"similarity_top_k": 5},
        )
        return SimpleComposableMemory(
            primary_memory=summary,
            secondary_memory_sources=[self._vector_memory],
        )

    @staticmethod
    def _message_payload(message: ChatMessage) -> dict[str, str]:
        role = getattr(message.role, "value", str(message.role))
        return {"role": role, "content": message.content or ""}

    async def search_memory(self, query: str) -> dict[str, Any]:
        messages = await self._vector_memory.aget(query.strip())
        return {
            "kind": "memory",
            "messages": [self._message_payload(message) for message in messages],
        }

    async def save_preference(self, preference: str) -> dict[str, Any]:
        value = preference.strip()
        if not value:
            raise ValueError("preference must not be empty")
        if value not in self._facts_block.facts:
            self._facts_block.facts.append(value)
        self._facts_signature = len(self._facts_block.facts)
        facts_path = self.conversation.dir_path / "facts.json"
        await asyncio.to_thread(
            facts_path.write_text,
            json.dumps({"facts": list(self._facts_block.facts)}, ensure_ascii=False),
            encoding="utf-8",
        )
        return {"kind": "preference", "saved": True, "preference": value}

    def navigation_knowledge(self, knowledge_name: str = ""):
        target = self.conversation.knowledge_name or knowledge_name.strip()
        if not target:
            raise ValueError("knowledge_name is required for global navigation")
        return self._kb.get(target)

    async def list_knowledges(self) -> dict[str, Any]:
        rows = await self._kb.alist()
        if self.conversation.type == "local":
            bound = self._kb.get(self.conversation.knowledge_name or "")
            rows = [row for row in rows if row["safe_name"] == bound.safe_name]
        return {"kind": "navigation", "items": rows}

    async def list_files(self, knowledge_name: str = "") -> dict[str, Any]:
        knowledge = self.navigation_knowledge(knowledge_name)
        return {
            "kind": "navigation",
            "knowledge": knowledge.name,
            "items": await asyncio.to_thread(knowledge.list_files),
        }

    async def list_sections(self, file_name: str, knowledge_name: str = "") -> dict[str, Any]:
        knowledge = self.navigation_knowledge(knowledge_name)
        items = await asyncio.to_thread(knowledge.list_sections, file_name)
        return {
            "kind": "navigation",
            "knowledge": knowledge.name,
            "file": file_name,
            "items": [{k: v for k, v in item.items() if k != "raw_content"} for item in items],
        }

    async def get_section_metadata(self, section_id: str, knowledge_name: str = "") -> dict[str, Any]:
        knowledge = self.navigation_knowledge(knowledge_name)
        section = await asyncio.to_thread(knowledge.get_section, section_id)
        if section is None:
            raise KeyError(f"section '{section_id}' does not exist in knowledge '{knowledge.name}'")
        return {"kind": "navigation", "knowledge": knowledge.name, "section": section}

    async def retrieve(self, mode: str, *, user_query: str, op_id: str, knowledge_name: str = "", full_retrieval: bool = False):
        if mode == LOCAL_EMBEDDED_RETRIEVAL:
            return await self._embedded.retrieve_local_context(knowledge_name=knowledge_name, user_query=user_query, op_id=op_id)
        if mode == LOCAL_HIERARCHICAL_RETRIEVAL:
            return await self._hierarchical.retrieve_local_context(knowledge_name=knowledge_name, user_query=user_query, op_id=op_id)
        if mode == LOCAL_AGREEMENT_RETRIEVAL:
            return await self._agreement.retrieve_local_context(knowledge_name=knowledge_name, user_query=user_query, op_id=op_id)
        if mode == LOCAL_VECTOR_CONDITIONED_RETRIEVAL:
            return await self._vector_conditioned.retrieve_local_context(knowledge_name=knowledge_name, user_query=user_query, op_id=op_id)
        if mode == GLOBAL_EMBEDDED_RETRIEVAL:
            return await self._embedded.retrieve_global_context(user_query=user_query, op_id=op_id)
        if mode == GLOBAL_HIERARCHICAL_RETRIEVAL:
            return await self._hierarchical.retrieve_global_context(user_query=user_query, full_retrieval=full_retrieval, op_id=op_id)
        if mode == GLOBAL_AGREEMENT_RETRIEVAL:
            return await self._agreement.retrieve_global_context(user_query=user_query, full_retrieval=full_retrieval, op_id=op_id)
        if mode == GLOBAL_VECTOR_CONDITIONED_RETRIEVAL:
            return await self._vector_conditioned.retrieve_global_context(user_query=user_query, op_id=op_id)
        raise ValueError(f"unknown retrieval mode: {mode}")

    async def reconstruct(self, result: Any, mode: str, op_id: str) -> list[dict] | None:
        return await self._reconstructor.reconstruct(result, mode, op_id=op_id)

    async def persist(self, handler: Any) -> None:
        summary = cast(ChatSummaryMemoryBuffer, self._memory.primary_memory)
        all_messages = summary.chat_store.get_messages("messages")
        new_messages = all_messages[self._messages_seen:]
        self._messages_seen = len(all_messages)
        if new_messages:
            await self._facts_block.aput(new_messages)
        await asyncio.to_thread(
            cast(SimpleChatStore, summary.chat_store).persist,
            str(self.conversation.messages_path),
        )
        await asyncio.to_thread(
            (self.conversation.dir_path / "facts.json").write_text,
            json.dumps({"facts": list(self._facts_block.facts)}, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            ctx_data = handler.ctx.to_dict()
            await asyncio.to_thread(
                self.conversation.context_path.write_text,
                json.dumps(ctx_data, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("context snapshot failed: %s", exc)

    async def set_title(self, title: str) -> None:
        await asyncio.to_thread(self.conversation.update_title, title)
        self.title_generated = True
