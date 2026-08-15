from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, cast
from uuid import uuid4

from llama_index.core.agent import FunctionAgent
from llama_index.core.agent.workflow.workflow_events import (
    AgentOutput,
    AgentStream,
    ToolCall,
    ToolCallResult,
)
from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.core.memory import (
    ChatSummaryMemoryBuffer,
    SimpleComposableMemory,
    VectorMemory,
)
from llama_index.core.memory.memory_blocks import FactExtractionMemoryBlock
from llama_index.core.storage.chat_store import SimpleChatStore
from llama_index.core.tools import FunctionTool
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.llms.ollama import Ollama
from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import QdrantClient

from .conversation_manager import Conversation
from .events import Event, EventBus, EventType
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

SYSTEM_PROMPT = """\
<|think|>
You are Raven, part of a RAG (Retrieval Augmented Generation) framework called RA\u2200EN (Retrieval Augmented Adaptive Epistemic Navigation),
An intelligent assistant whos primary function is to help the users navigate the knowledge base using a wide range of retrieval tools. You're strictly prohibited to use your training data to answer questions.
You have access to a knowledge base containing multiple knowledges. Each knowledge is a separate database containing multiple user uploaded files.
Users can create 'local' or 'global' conversations to interact with you. In 'local' conversations you only have access to local tools that can only navigate a single specified knowledge. In 'global' conversations you have access to both local and global tools unless the the user constrains you to use only one tool.
Prohibitions:
    1. You're strictly prohibited to use your training data to answer questions.
    2. You're strictly prohibited to use 'local' tools without passing a 'knowledge_name' argument. You're prohibited to reuse 'knowledge_name' from a previous message.
    3. You're strictly prohibited to invoke tools that you don't have access to right now.

Current Conversation type: {conversation_type}
Knowledge name: {knowledge_name}

Constrained mode check:
    1. Check the number of available tools.
    2. If the answer is 1, that means this query is in constrained mode and you must use the retrieval tool that is available.

When you get a query from user you'll follow the following steps and you must follow these protocols step by step:
0. Read the prohibitions right now. You must not violate them in any circumstances.
1. Identify if the query is casual or does it requires a tool call.
2. Do not invoke tool calls for casual conversations.
3. Critical: You MUST invoke a retrieval tool for EVERY substantive question before answering. Retrieval is your only source of knowledge — you are strictly forbidden from using your training data or general knowledge to answer. If the retrieved context does not answer the question, say so honestly.
4. Critical: Perform the Constrained mode check right now.
5. Critical: Identify if you're about to use a local tool or not. You must ask the user to provide a 'knowledge_name' for local tools if none is provided. Do not invoke a local tool without a knowledge name.
6. You must act as a presenter who is presenting the retrieved context and explaining every detail.
"""

TOOL_DESCRIPTIONS: dict[str, str] = {
    LOCAL_EMBEDDED_RETRIEVAL: (
        "Retrieves relevant context from a specific knowledge using vector similarity search. "
        "Use for precise queries — scientific, engineering, medical or technical data."
    ),
    LOCAL_HIERARCHICAL_RETRIEVAL: (
        "Retrieves relevant context from a specific knowledge using reasoning over metadata. "
        "Use for narrative queries — stories, events, characters."
    ),
    LOCAL_AGREEMENT_RETRIEVAL: (
        "Retrieves context from a specific knowledge using both vector and hierarchical retrieval "
        "and checks agreement between them. Use when high confidence is required."
    ),
    LOCAL_VECTOR_CONDITIONED_RETRIEVAL: (
        "Retrieves context from a specific knowledge by first narrowing search to most relevant files "
        "then applying hierarchical reasoning. Use when you want targeted retrieval within a knowledge."
    ),
    GLOBAL_EMBEDDED_RETRIEVAL: (
        "Retrieves relevant context across all knowledges using vector similarity search. "
        "Use when the user wants to search broadly without specifying a knowledge."
    ),
    GLOBAL_HIERARCHICAL_RETRIEVAL: (
        "Retrieves relevant context across all knowledges using reasoning over metadata. "
        "Use for narrative queries across multiple knowledges. Set full_retrieval to true to search "
        "all knowledges, false to let the model select the most relevant knowledges first."
    ),
    GLOBAL_AGREEMENT_RETRIEVAL: (
        "Retrieves context across all knowledges using both vector and hierarchical retrieval and checks "
        "agreement. Use when high confidence is needed across multiple knowledges."
    ),
    GLOBAL_VECTOR_CONDITIONED_RETRIEVAL: (
        "Retrieves context across all knowledges by first narrowing search using vector retrieval then "
        "applying hierarchical reasoning on the most relevant files."
    ),
}

RETRIEVAL_TOOLS = set(TOOL_DESCRIPTIONS.keys())
LOCAL_TOOL_NAMES = {
    LOCAL_EMBEDDED_RETRIEVAL,
    LOCAL_HIERARCHICAL_RETRIEVAL,
    LOCAL_AGREEMENT_RETRIEVAL,
    LOCAL_VECTOR_CONDITIONED_RETRIEVAL,
}
GLOBAL_TOOL_NAMES = {
    GLOBAL_EMBEDDED_RETRIEVAL,
    GLOBAL_HIERARCHICAL_RETRIEVAL,
    GLOBAL_AGREEMENT_RETRIEVAL,
    GLOBAL_VECTOR_CONDITIONED_RETRIEVAL,
}


class ChatSession:
    """The session entrypoint: wraps ONE FunctionAgent.

    Receives loaded llm/embed_model from ModelManager — never loads models itself.
    Tools are filtered by conversation type: local conversations get the 4
    local_* retrieval tools, global conversations get all 8.

    Memory stack (llama-index 0.14 API):
    - primary: ChatSummaryMemoryBuffer (auto-summarize; replaces v1 _trim_messages)
      backed by a SimpleChatStore persisted to messages.json. The sliding context
      window is llama-index managed (token_limit); pass None to keep the derived
      default or an int to override it for a given hardware profile.
    - secondary: VectorMemory over a per-conversation local Qdrant dir (replaces
      v1 get_memory). VectorMemory is a BaseMemory, so it fits
      SimpleComposableMemory.secondary_memory_sources.
    - FactExtractionMemoryBlock is a BaseMemoryBlock (NOT a BaseMemory) so it
      cannot be a secondary source here; it is managed separately and its facts
      are injected into the system prompt as [PREFERENCES] (v1 semantics).
    """

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

        self._op_id: str | None = None
        self.title_generated = conversation.title != "New Conversation"

        self._facts_block = FactExtractionMemoryBlock(llm=self._llm)
        self._facts_signature = 0
        self._load_facts()
        self._memory = self._build_memory()
        self._messages_seen = len(
            cast(ChatSummaryMemoryBuffer, self._memory.primary_memory)
            .chat_store.get_messages("messages")
        )
        self._tools = self._build_tools()
        self._agent = self._build_agent(self._tools)

    # ---- lifecycle ----------------------------------------------------

    def close(self) -> None:
        """Release the per-conversation Qdrant lock (local mode is single-instance)."""
        try:
            self._qdrant_client.close()
        except Exception:
            pass

    # ---- memory -------------------------------------------------------

    def _load_facts(self) -> None:
        facts_path = self.conversation.dir_path / "facts.json"
        if facts_path.exists():
            try:
                data = json.loads(facts_path.read_text(encoding="utf-8"))
                self._facts_block.facts = list(data.get("facts", []))
            except Exception:
                pass

    def _build_memory(self) -> SimpleComposableMemory:
        chat_store = SimpleChatStore()
        if self.conversation.messages_path.exists():
            chat_store = SimpleChatStore.from_persist_path(
                str(self.conversation.messages_path)
            )

        summary = ChatSummaryMemoryBuffer.from_defaults(
            llm=self._llm,
            chat_store=chat_store,
            chat_store_key="messages",
            token_limit=self._token_limit,
        )

        self.conversation.qdrant_dir.mkdir(parents=True, exist_ok=True)
        qdrant_client = QdrantClient(path=str(self.conversation.qdrant_dir))
        self._qdrant_client = qdrant_client
        vector_store = QdrantVectorStore(
            collection_name="messages",
            client=qdrant_client,
        )
        if not vector_store._collection_exists("messages"):
            probe = self._embed_model.get_text_embedding("raven memory probe")
            vector_store._create_collection("messages", len(probe))
        vector_memory = VectorMemory.from_defaults(
            vector_store=vector_store,
            embed_model=self._embed_model,
        )

        return SimpleComposableMemory(
            primary_memory=summary,
            secondary_memory_sources=[vector_memory],
        )

    # ---- tools --------------------------------------------------------

    def _build_tools(self) -> list[FunctionTool]:
        tools: list[FunctionTool] = []
        local = self.conversation.type == "local"
        for mode in sorted(TOOL_DESCRIPTIONS.keys()):
            if local and mode in GLOBAL_TOOL_NAMES:
                continue
            tools.append(self._make_tool(mode))
        return tools

    def _make_tool(self, mode: str) -> FunctionTool:
        local = mode in LOCAL_TOOL_NAMES
        full_retrieval = mode in {
            GLOBAL_HIERARCHICAL_RETRIEVAL,
            GLOBAL_AGREEMENT_RETRIEVAL,
        }

        async def _local_call(user_query: str, knowledge_name: str = "") -> str:
            target = self.conversation.knowledge_name or knowledge_name
            if not target:
                return (
                    "Local tools need a 'knowledge_name'. None provided. "
                    "Ask the user to provide 'knowledge_name' immediately"
                )
            result = await self._retrieve(
                mode, user_query=user_query, knowledge_name=target
            )
            return await self._tool_result(mode, result)

        async def _global_call(user_query: str) -> str:
            result = await self._retrieve(mode, user_query=user_query)
            return await self._tool_result(mode, result)

        async def _global_call_full(user_query: str, full_retrieval: bool = False) -> str:
            result = await self._retrieve(
                mode, user_query=user_query, full_retrieval=full_retrieval
            )
            return await self._tool_result(mode, result)

        fn = _local_call
        if not local:
            fn = _global_call_full if full_retrieval else _global_call
        return FunctionTool.from_defaults(
            async_fn=fn,
            name=mode,
            description=TOOL_DESCRIPTIONS[mode],
        )

    async def _retrieve(self, mode: str, **kwargs: Any):
        if mode == LOCAL_EMBEDDED_RETRIEVAL:
            return await self._embedded.retrieve_local_context(
                knowledge_name=kwargs["knowledge_name"],
                user_query=kwargs["user_query"],
                op_id=self._op_id,
            )
        if mode == LOCAL_HIERARCHICAL_RETRIEVAL:
            return await self._hierarchical.retrieve_local_context(
                knowledge_name=kwargs["knowledge_name"],
                user_query=kwargs["user_query"],
                op_id=self._op_id,
            )
        if mode == LOCAL_AGREEMENT_RETRIEVAL:
            return await self._agreement.retrieve_local_context(
                knowledge_name=kwargs["knowledge_name"],
                user_query=kwargs["user_query"],
                op_id=self._op_id,
            )
        if mode == LOCAL_VECTOR_CONDITIONED_RETRIEVAL:
            return await self._vector_conditioned.retrieve_local_context(
                knowledge_name=kwargs["knowledge_name"],
                user_query=kwargs["user_query"],
                op_id=self._op_id,
            )
        if mode == GLOBAL_EMBEDDED_RETRIEVAL:
            return await self._embedded.retrieve_global_context(
                user_query=kwargs["user_query"],
                op_id=self._op_id,
            )
        if mode == GLOBAL_HIERARCHICAL_RETRIEVAL:
            return await self._hierarchical.retrieve_global_context(
                user_query=kwargs["user_query"],
                full_retrieval=kwargs.get("full_retrieval", False),
                op_id=self._op_id,
            )
        if mode == GLOBAL_AGREEMENT_RETRIEVAL:
            return await self._agreement.retrieve_global_context(
                user_query=kwargs["user_query"],
                full_retrieval=kwargs.get("full_retrieval", False),
                op_id=self._op_id,
            )
        if mode == GLOBAL_VECTOR_CONDITIONED_RETRIEVAL:
            return await self._vector_conditioned.retrieve_global_context(
                user_query=kwargs["user_query"],
                op_id=self._op_id,
            )
        raise ValueError(f"unknown retrieval mode: {mode}")

    async def _tool_result(self, mode: str, result) -> str:
        cards = await self._reconstructor.reconstruct(result, mode, op_id=self._op_id)
        if cards:
            for card in cards:
                for sec in card["sections"]:
                    if sec["highlighted"]:
                        self._emit(
                            EventType.CHAT_RETRIEVED,
                            {
                                "knowledge": card["knowledge_name"],
                                "file": card["file_name"],
                                "section_id": sec["section_id"],
                            },
                        )
        return json.dumps(result, ensure_ascii=False)

    # ---- agent --------------------------------------------------------

    def _build_agent(
        self, tools: list[FunctionTool], initial_tool_choice: str | None = None
    ) -> FunctionAgent:
        return FunctionAgent(
            name="Raven",
            description="Raven RAG assistant",
            system_prompt=self._system_prompt(),
            tools=tools,
            llm=self._llm,
            streaming=True,
            initial_tool_choice=initial_tool_choice,
        )

    def _system_prompt(self) -> str:
        preferences = "\n".join(
            f"<fact>{fact}</fact>" for fact in self._facts_block.facts
        )
        preferences_block = (
            f"\n\n[PREFERENCES]\n{preferences}\n[/PREFERENCES]" if preferences else ""
        )
        return SYSTEM_PROMPT.format(
            conversation_type=self.conversation.type,
            knowledge_name=self.conversation.knowledge_name or "None",
        ) + preferences_block

    # ---- events -------------------------------------------------------

    def _emit(self, etype: EventType, data: dict) -> None:
        self._bus.publish(Event(type=etype, data=data, op_id=self._op_id))

    # ---- lifecycle ----------------------------------------------------

    async def stream(
        self,
        user_text: str,
        retrieval_mode: str = "auto",
        op_id: str | None = None,
    ) -> str:
        """Run one turn. Emits chat.* events on the bus, returns the final reply."""
        op_id = op_id or uuid4().hex
        self._op_id = op_id
        try:
            if not self.title_generated:
                await self._generate_title(user_text)
                self.title_generated = True

            agent = self._agent
            if retrieval_mode and retrieval_mode != "auto":
                if retrieval_mode not in RETRIEVAL_TOOLS:
                    raise ValueError(f"unknown retrieval_mode: {retrieval_mode}")
                single = [t for t in self._tools if t.metadata.name == retrieval_mode]
                agent = self._build_agent(single, initial_tool_choice=retrieval_mode)

            handler = agent.run(user_msg=user_text, memory=self._memory)

            final_output: AgentOutput | None = None
            async for ev in handler.stream_events():
                if isinstance(ev, AgentStream):
                    if ev.thinking_delta:
                        self._emit(
                            EventType.CHAT_DELTA,
                            {"kind": "thinking_chunk", "delta": ev.thinking_delta},
                        )
                    if ev.delta:
                        self._emit(
                            EventType.CHAT_DELTA,
                            {"kind": "response_chunk", "delta": ev.delta},
                        )
                elif isinstance(ev, ToolCall):
                    self._emit(
                        EventType.CHAT_TOOL_CALL,
                        {"name": ev.tool_name, "args": ev.tool_kwargs},
                    )
                elif isinstance(ev, ToolCallResult):
                    self._emit(
                        EventType.CHAT_TOOL_RESULT,
                        {
                            "name": ev.tool_name,
                            "ok": not ev.tool_output.is_error,
                            "error": str(ev.tool_output.raw_output)
                            if ev.tool_output.is_error
                            else None,
                        },
                    )
                elif isinstance(ev, AgentOutput):
                    final_output = ev

            stop = await handler
            if isinstance(stop, AgentOutput):
                final_output = stop
            else:
                final_output = stop.result if hasattr(stop, "result") else final_output

            await self._persist(handler)

            reply = ""
            if final_output is not None and final_output.response is not None:
                reply = final_output.response.content or ""
            self._emit(EventType.CHAT_COMPLETE, {"reply": reply})
            return reply
        except Exception as exc:
            self._emit(EventType.ERROR, {"op": "chat", "error": str(exc)})
            raise

    # ---- persistence --------------------------------------------------

    async def _persist(self, handler) -> None:
        summary = cast(ChatSummaryMemoryBuffer, self._memory.primary_memory)
        all_messages = summary.chat_store.get_messages("messages")
        new_messages = all_messages[self._messages_seen :]
        self._messages_seen = len(all_messages)
        if new_messages:
            await self._facts_block.aput(new_messages)
        await asyncio.to_thread(
            cast(SimpleChatStore, summary.chat_store).persist,
            str(self.conversation.messages_path),
        )
        facts_path = self.conversation.dir_path / "facts.json"
        await asyncio.to_thread(
            facts_path.write_text,
            json.dumps({"facts": list(self._facts_block.facts)}, ensure_ascii=False),
            encoding="utf-8",
        )
        if len(self._facts_block.facts) != self._facts_signature:
            self._facts_signature = len(self._facts_block.facts)
            self._agent = self._build_agent(self._tools)
        try:
            ctx_data = handler.ctx.to_dict()
            await asyncio.to_thread(
                self.conversation.context_path.write_text,
                json.dumps(ctx_data, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"context snapshot failed: {exc}")

    # ---- title --------------------------------------------------------

    async def _generate_title(self, first_message: str) -> str | None:
        try:
            response = await self._llm.achat(
                messages=[
                    ChatMessage(
                        role=MessageRole.SYSTEM,
                        content="Generate a short 4-6 word title for a conversation "
                        "that starts with the following message. Output only the "
                        "title, nothing else.",
                    ),
                    ChatMessage(role=MessageRole.USER, content=first_message),
                ]
            )
            title = (response.message.content or "").strip()
            if title:
                await asyncio.to_thread(self.conversation.update_title, title)
                self._emit(
                    EventType.CONVERSATION_TITLE_GENERATED,
                    {"conversation_id": self.conversation.conversation_id, "title": title},
                )
                return title
        except Exception as exc:
            logger.warning(f"failed to auto-generate title: {exc}")
        return None