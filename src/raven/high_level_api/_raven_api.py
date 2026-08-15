"""High-level facade for the v2 backend.

The Raven class automates everything ``examples/interact_session.py`` wires by
hand: the ModelManager, KnowledgeBase, the four retrieval pipelines, the
Reconstructor and the ConversationManager — then exposes a small async surface
(``stream`` / ``ingest`` / CRUD / model ops) that hides the event-bus op_id
plumbing. ``chat`` is streaming-only in v2; there is no blocking
``generate_response``.

Lifecycle::

    api = Raven()
    await api.start()
    ...
    await api.close()

Chat::

    async for ev in api.stream(conversation_id, "What is a reaction wheel?"):
        # same raw Event objects as bus.subscribe, but no op_id to manage.
        # ends on CHAT_COMPLETE (data["reply"]) or ERROR (also re-raised).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

from ..chat_session import ChatSession
from ..conversation_manager import Conversation, ConversationManager
from ..events import Event, EventBus, EventType
from ..knowledge import KnowledgeBase
from ..local_ollama import LocalOllama
from ..model import ModelManager
from ..pipeline import (
    AgreementBasedRetrievalPipeline,
    EmbeddedRetrievalPipeline,
    HierarchicalRetrievalPipeline,
    IngestionPipeline,
    VectorConditionedRetrievalPipeline,
)
from ..reconstructor import Reconstructor

_CHAT_DONE = (EventType.CHAT_COMPLETE, EventType.ERROR)
_PULL_DONE = (EventType.MODEL_PULL_COMPLETE, EventType.ERROR)


class Raven:
    def __init__(
        self,
        base_model: str | None = None,
        embed_model: str | None = None,
        server: LocalOllama | None = None,
        bus: EventBus | None = None,
        knowledge_base_path: Path | None = None,
        conversation_dir: Path | None = None,
        token_limit: int | None = None,
    ) -> None:
        self.bus = bus or EventBus()
        self._base_model = base_model
        self._embed_model = embed_model
        self._mm = ModelManager(self.bus, server=server)
        self._knowledge_base_path = knowledge_base_path
        self._conversation_dir = conversation_dir
        self._token_limit = token_limit

        self._kb: KnowledgeBase | None = None
        self._mgr: ConversationManager | None = None
        self._ingestion: IngestionPipeline | None = None
        self._embedded: EmbeddedRetrievalPipeline | None = None
        self._hierarchical: HierarchicalRetrievalPipeline | None = None
        self._agreement: AgreementBasedRetrievalPipeline | None = None
        self._vector_conditioned: VectorConditionedRetrievalPipeline | None = None
        self._reconstructor: Reconstructor | None = None
        self._llm: Any = None
        self._embed: Any = None

        # ONE live ChatSession at a time: each holds a lock on its conversation's
        # local Qdrant dir, so switching conversations closes the previous one.
        self._cached_session: ChatSession | None = None
        self._cached_id: str | None = None
        self._started = False

    @property
    def is_started(self) -> bool:
        return self._started

    # ---- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        await self._mm.start()
        try:
            embed = await self._mm.load_embed_model(self._embed_model)
            llm = await self._mm.load_base_model(self._base_model)
            self._embed = embed
            self._llm = llm

            kb = KnowledgeBase(
                self.bus,
                knowledge_base_path=self._knowledge_base_path,
                embed_model=embed,
            )
            await kb.start()
            self._kb = kb

            self._embedded = EmbeddedRetrievalPipeline(kb, embed, self.bus)
            self._hierarchical = HierarchicalRetrievalPipeline(kb, llm, self.bus)
            self._agreement = AgreementBasedRetrievalPipeline(
                kb, self._embedded, self._hierarchical, self.bus
            )
            self._vector_conditioned = VectorConditionedRetrievalPipeline(
                kb, self._embedded, self._hierarchical, self.bus
            )
            self._reconstructor = Reconstructor(kb, self.bus)
            self._ingestion = IngestionPipeline(self.bus, kb, llm, embed)

            mgr = ConversationManager(
                self.bus,
                conversation_dir=self._conversation_dir,
                knowledge_base=kb,
            )
            await mgr.start()
            self._mgr = mgr
        except Exception:
            if self._kb is not None:
                await self._kb.close()
            await self._mm.stop()
            raise
        self._started = True

    async def close(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._cached_session is not None:
            self._cached_session.close()
            self._cached_session = None
            self._cached_id = None
        if self._kb is not None:
            await self._kb.close()
            self._kb = None
        await self._mm.stop()

    # ---- knowledge CRUD -----------------------------------------------

    async def create_knowledge(self, name: str, user_summary: str = ""):
        return await self._require_kb().create(name, user_summary)

    async def delete_knowledge(self, name: str) -> None:
        await self._require_kb().delete(name)

    def list_knowledges(self) -> list[dict[str, Any]]:
        return self._require_kb().list()

    def list_files(self, knowledge_name: str) -> list[dict[str, Any]]:
        return self._require_kb().get(knowledge_name).list_files()

    def list_sections(self, knowledge_name: str, file_name: str) -> list[dict[str, Any]]:
        return self._require_kb().get(knowledge_name).list_sections(file_name)

    async def set_knowledge_summary(self, name: str, summary: str) -> None:
        self._require_kb().get(name).set_summary(summary)

    # ---- ingestion ----------------------------------------------------

    async def ingest(self, knowledge_name: str, file_path: str) -> int:
        """Ingest one file, blocking until done. Returns the chunk count."""
        op_id = await self._require_ingestion().ingest(knowledge_name, file_path)
        for ev in self.bus.history(op_id=op_id):
            if ev.type is EventType.ERROR:
                raise RuntimeError(ev.data.get("error", "ingestion failed"))
            if ev.type is EventType.INGESTION_COMPLETE:
                return ev.data.get("count", 0)
        async for ev in self.bus.subscribe(op_id=op_id):
            if ev.type is EventType.ERROR:
                raise RuntimeError(ev.data.get("error", "ingestion failed"))
            if ev.type is EventType.INGESTION_COMPLETE:
                return ev.data.get("count", 0)
        raise RuntimeError("ingestion finished without a completion event")

    # ---- conversations ------------------------------------------------

    def list_conversations(self) -> list[dict[str, Any]]:
        return self._require_mgr().list()

    def get_conversation(self, conversation_id: str) -> Conversation:
        return self._require_mgr().get(conversation_id)

    async def create_conversation(self, knowledge_name: str | None = None) -> Conversation:
        return await self._require_mgr().create(knowledge_name=knowledge_name)

    async def delete_conversation(self, conversation_id: str) -> None:
        if self._cached_id == conversation_id and self._cached_session is not None:
            self._cached_session.close()
            self._cached_session = None
            self._cached_id = None
        await self._require_mgr().delete(conversation_id)

    async def rename_conversation(self, conversation_id: str, title: str) -> Conversation:
        return await self._require_mgr().rename(conversation_id, title)

    async def toggle_pin(self, conversation_id: str) -> Conversation:
        return await self._require_mgr().toggle_pin(conversation_id)

    # ---- session / chat -----------------------------------------------

    async def session(
        self,
        conversation_id: str | None = None,
        knowledge_name: str | None = None,
    ) -> ChatSession:
        """Get (creating if needed) the live ChatSession for a conversation.

        ``conversation_id=None`` creates a new conversation; a valid id loads
        it. Only one session is kept open at a time (each holds a local Qdrant
        lock), so switching conversations closes the previous one.
        """
        return await self._get_session(conversation_id, knowledge_name)

    async def stream(
        self,
        user_text: str,
        conversation_id: str | None = None,
        knowledge_name: str | None = None,
        retrieval_mode: str = "auto",
    ) -> AsyncIterator[Event]:
        """Run one chat turn, yielding raw ``chat.*`` Event objects.

        Ends after CHAT_COMPLETE (final event carries ``data["reply"]``) or
        ERROR. Terminal errors are also re-raised from the generator.
        """
        self._require_started()
        sess = await self._get_session(conversation_id, knowledge_name)
        op_id = uuid4().hex
        task = asyncio.create_task(
            sess.stream(user_text, retrieval_mode=retrieval_mode, op_id=op_id)
        )
        try:
            async for ev in self._collect(op_id, _CHAT_DONE):
                yield ev
            await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    # ---- models -------------------------------------------------------

    async def list_models(self) -> list[dict[str, Any]]:
        return await self._mm.list_models()

    async def inspect(self, model: str) -> dict[str, Any]:
        return await self._mm.inspect(model)

    async def delete_model(self, model: str) -> None:
        await self._mm.delete(model)

    async def pull(self, model: str) -> AsyncIterator[Event]:
        """Pull a model, yielding MODEL_PULL_* events until complete/error."""
        self._require_started()
        op_id = await self._mm.pull(model)
        async for ev in self._collect(op_id, _PULL_DONE):
            yield ev

    async def unload_embed_model(self, model: str | None = None) -> None:
        await self._mm.unload_embed_model(model)

    async def unload_base_model(self, model: str | None = None) -> None:
        await self._mm.unload_base_model(model)

    # ---- internals ----------------------------------------------------

    async def _get_session(
        self,
        conversation_id: str | None,
        knowledge_name: str | None,
    ) -> ChatSession:
        self._require_started()
        mgr = self._require_mgr()
        if conversation_id is None:
            conversation_id = (await mgr.create(knowledge_name=knowledge_name)).conversation_id
        if self._cached_id == conversation_id and self._cached_session is not None:
            return self._cached_session
        if self._cached_session is not None:
            self._cached_session.close()
        conv = mgr.get(conversation_id)
        session = await asyncio.to_thread(
            ChatSession,
            knowledge_base=self._require_kb(),
            bus=self.bus,
            conversation=conv,
            llm=self._llm,
            embed_model=self._embed,
            embedded_pipeline=self._require_embedded(),
            hierarchical_pipeline=self._require_hierarchical(),
            agreement_pipeline=self._require_agreement(),
            vector_conditioned_pipeline=self._require_vector_conditioned(),
            reconstructor=self._require_reconstructor(),
            token_limit=self._token_limit,
        )
        self._cached_session = session
        self._cached_id = conversation_id
        return session

    async def _collect(
        self,
        op_id: str,
        done: tuple[EventType, ...],
    ) -> AsyncIterator[Event]:
        """Replay already-published events for op_id, then stream new ones."""
        for ev in self.bus.history(op_id=op_id):
            yield ev
            if ev.type in done:
                return
        async for ev in self.bus.subscribe(op_id=op_id):
            yield ev
            if ev.type in done:
                return

    # ---- guards -------------------------------------------------------

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("Raven not started: call await raven.start() first")

    def _require_kb(self) -> KnowledgeBase:
        self._require_started()
        assert self._kb is not None
        return self._kb

    def _require_mgr(self) -> ConversationManager:
        self._require_started()
        assert self._mgr is not None
        return self._mgr

    def _require_ingestion(self) -> IngestionPipeline:
        self._require_started()
        assert self._ingestion is not None
        return self._ingestion

    def _require_embedded(self) -> EmbeddedRetrievalPipeline:
        self._require_started()
        assert self._embedded is not None
        return self._embedded

    def _require_hierarchical(self) -> HierarchicalRetrievalPipeline:
        self._require_started()
        assert self._hierarchical is not None
        return self._hierarchical

    def _require_agreement(self) -> AgreementBasedRetrievalPipeline:
        self._require_started()
        assert self._agreement is not None
        return self._agreement

    def _require_vector_conditioned(self) -> VectorConditionedRetrievalPipeline:
        self._require_started()
        assert self._vector_conditioned is not None
        return self._vector_conditioned

    def _require_reconstructor(self) -> Reconstructor:
        self._require_started()
        assert self._reconstructor is not None
        return self._reconstructor
