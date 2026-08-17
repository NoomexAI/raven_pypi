from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from collections import deque
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
from qdrant_client.http import models as qdrant_models

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

_LEGACY_SYSTEM_PROMPT = """\
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

SYSTEM_PROMPT = """\
<|think|>
You are Raven, the grounded assistant in RAVEN (Retrieval Augmented Adaptive Epistemic Navigation).

RAVEN answers from the user's knowledge base and conversation memory. Do not use general knowledge or model training knowledge as evidence for a knowledge-base answer. If the available evidence does not answer the user's question, say so clearly instead of guessing.

RAVEN has separate knowledges. Each knowledge contains user-provided files and sections. The current conversation context is:
- Conversation type: {conversation_type}
- Bound knowledge: {knowledge_name}

CONVERSATION SCOPE

Local conversation:
- The conversation is bound to the named knowledge.
- Use only local retrieval tools for that knowledge.
- Do not search or navigate another knowledge.
- Local retrieval and navigation tools may omit knowledge_name because the bound knowledge is already authoritative.

Global conversation:
- Global retrieval tools search across the knowledge base.
- Local retrieval and navigation tools require an explicit knowledge_name for the current tool call.
- Do not infer or silently reuse a knowledge_name from an earlier user message.

TOOL FAMILIES

Retrieval tools answer questions from knowledge-base content:
- Embedded retrieval uses vector similarity for precise technical queries.
- Hierarchical retrieval uses section metadata and reasoning for narrative or structured queries.
- Agreement retrieval combines vector and hierarchical results when confidence matters.
- Vector-conditioned retrieval narrows relevant files before hierarchical retrieval.
- Local tools search one knowledge; global tools search across knowledges.

Navigation tools explore the knowledge-base structure and are not substitutes for retrieval:
- list_knowledges discovers available knowledges.
- list_files discovers files in a knowledge.
- list_sections discovers section metadata in a file.
- get_section_metadata retrieves one section's metadata and its raw content.
Use navigation before retrieval when you need to discover where information lives. If the user explicitly asks to inspect a known section, use get_section_metadata directly. Do not return raw content from list_sections when a specific section must be read.

Memory tools operate on this conversation, not on the knowledge base:
- get_memory searches relevant past conversation messages when earlier context is no longer visible.
- save_preference stores an explicit user preference, instruction, or specification for future turns. Do not save assumptions or ordinary facts as preferences.
Do not use conversation memory as evidence for a knowledge-base claim unless the user is asking about the conversation itself.

TURN POLICY

1. Decide whether the message is casual, a conversation-memory request, a navigation request, or a knowledge question.
2. Do not call tools for simple casual conversation.
3. For a substantive knowledge question, use an appropriate retrieval tool before answering. You may use navigation first if you need to locate the relevant knowledge, file, or section.
4. For a request about available knowledges, files, sections, metadata, or a specific section's text, use navigation tools instead of guessing.
5. For references to earlier discussion, use get_memory when the needed context is not already available.
6. If retrieval_mode has constrained the turn to one retrieval tool, use that retrieval tool; memory and navigation tools remain available when needed.
7. Use only tools that are actually provided in the current session. Never invent a tool, tool result, file, section, or citation.
8. After tools return, base the answer only on their results. Identify sources using knowledge name, file name, and section ID when available.
9. If a tool returns no relevant evidence, explain that the knowledge base did not provide enough information. Do not fill the gap with general knowledge.

Present retrieved information clearly and explain it faithfully. Keep the answer focused on the user's request.
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
MEMORY_TOOL_NAMES = {"get_memory", "save_preference"}
NAVIGATION_TOOL_NAMES = {
    "list_knowledges",
    "list_files",
    "list_sections",
    "get_section_metadata",
}
NON_RETRIEVAL_TOOL_NAMES = MEMORY_TOOL_NAMES | NAVIGATION_TOOL_NAMES
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


def _tool_result_summary(reconstructed: list[dict] | None) -> dict[str, Any]:
    """Build the bounded, UI-safe part of a retrieval tool result."""
    files: list[dict[str, Any]] = []
    section_count = 0
    for card in reconstructed or []:
        section_ids = [
            section["section_id"]
            for section in card.get("sections", [])
            if section.get("highlighted") and section.get("section_id")
        ]
        if not section_ids:
            continue
        section_count += len(section_ids)
        files.append(
            {
                "knowledge": card.get("knowledge_name"),
                "file": card.get("file_name"),
                "section_ids": section_ids,
            }
        )
    return {
        "kind": "retrieval",
        "file_count": len(files),
        "section_count": section_count,
        "files": files,
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

        self._op_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            "raven_chat_operation_id", default=None
        )
        self.title_generated = conversation.title != "New Conversation"

        self._facts_block = FactExtractionMemoryBlock(llm=self._llm)
        self._vector_memory: VectorMemory | None = None
        self._pending_tool_results: dict[str, deque[dict[str, Any]]] = {}
        self._facts_signature = 0
        self._load_facts()
        self._memory = self._build_memory()
        self._messages_seen = len(
            cast(ChatSummaryMemoryBuffer, self._memory.primary_memory)
            .chat_store.get_messages("messages")
        )
        self._tools = self._build_tools()
        self._agent = self._build_agent(self._tools)

    @classmethod
    async def create(cls, **kwargs: Any) -> "ChatSession":
        """Construct a session without blocking the event loop."""
        return await asyncio.to_thread(cls, **kwargs)

    # ---- lifecycle ----------------------------------------------------

    def close(self) -> None:
        """Release the per-conversation Qdrant lock (local mode is single-instance)."""
        try:
            self._qdrant_client.close()
        except Exception:
            pass

    async def aclose(self) -> None:
        """Close the Qdrant client without blocking the event loop."""
        await asyncio.to_thread(self.close)

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

        # LlamaIndex's current Qdrant adapter queries the default named vector
        # (``text-dense``).  Creating a collection with the low-level Qdrant
        # client keeps the collection schema aligned with that adapter.  Older
        # Raven conversations may still contain an unnamed legacy collection;
        # QdrantVectorStore detects that format when it is constructed below.
        memory_vector_name = "text-dense"
        if not qdrant_client.collection_exists("messages"):
            probe = self._embed_model.get_text_embedding("raven memory probe")
            qdrant_client.create_collection(
                collection_name="messages",
                vectors_config={
                    memory_vector_name: qdrant_models.VectorParams(
                        size=len(probe),
                        distance=qdrant_models.Distance.COSINE,
                    )
                },
            )
        vector_store = QdrantVectorStore(
            collection_name="messages",
            client=qdrant_client,
            dense_vector_name=memory_vector_name,
        )
        vector_memory = VectorMemory.from_defaults(
            vector_store=vector_store,
            embed_model=self._embed_model,
            retriever_kwargs={"similarity_top_k": 5},
        )
        self._vector_memory = vector_memory

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
        tools.extend(self._build_memory_tools())
        tools.extend(self._build_navigation_tools())
        return tools

    def _queue_tool_result(self, tool_name: str, result: dict[str, Any]) -> None:
        self._pending_tool_results.setdefault(tool_name, deque()).append(result)

    @staticmethod
    def _message_payload(message: ChatMessage) -> dict[str, str]:
        role = getattr(message.role, "value", str(message.role))
        return {"role": role, "content": message.content or ""}

    async def _memory_search(self, query: str) -> str:
        if self._vector_memory is None:
            raise RuntimeError("conversation vector memory is not initialized")
        messages = await self._vector_memory.aget(query.strip())
        payload = {
            "kind": "memory",
            "messages": [self._message_payload(message) for message in messages],
        }
        self._queue_tool_result("get_memory", payload)
        return json.dumps(payload, ensure_ascii=False)

    async def _save_preference(self, preference: str) -> str:
        value = preference.strip()
        if not value:
            raise ValueError("preference must not be empty")
        if value not in self._facts_block.facts:
            self._facts_block.facts.append(value)
        self._facts_signature = len(self._facts_block.facts)
        self._agent = self._build_agent(self._tools)
        facts_path = self.conversation.dir_path / "facts.json"
        await asyncio.to_thread(
            facts_path.write_text,
            json.dumps({"facts": list(self._facts_block.facts)}, ensure_ascii=False),
            encoding="utf-8",
        )
        payload = {"kind": "preference", "saved": True, "preference": value}
        self._queue_tool_result("save_preference", payload)
        return json.dumps(payload, ensure_ascii=False)

    def _build_memory_tools(self) -> list[FunctionTool]:
        async def get_memory(query: str) -> str:
            """Search relevant messages from this conversation's vector memory."""
            return await self._memory_search(query)

        async def save_preference(preference: str) -> str:
            """Save an explicit user preference for future turns."""
            return await self._save_preference(preference)

        return [
            FunctionTool.from_defaults(
                async_fn=get_memory,
                name="get_memory",
                description=(
                    "Searches relevant messages from this conversation's past memory. "
                    "Use when the user refers to something outside the current context window."
                ),
            ),
            FunctionTool.from_defaults(
                async_fn=save_preference,
                name="save_preference",
                description=(
                    "Saves an explicit user preference, instruction, or specification "
                    "for this conversation."
                ),
            ),
        ]

    def _navigation_knowledge(self, knowledge_name: str = ""):
        target = self.conversation.knowledge_name or knowledge_name.strip()
        if not target:
            raise ValueError("knowledge_name is required for global navigation")
        return self._kb.get(target)

    @staticmethod
    def _metadata_only(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key != "raw_content"}

    async def _list_knowledges(self) -> str:
        rows = await self._kb.alist()
        if self.conversation.type == "local":
            bound = self._kb.get(self.conversation.knowledge_name or "")
            rows = [row for row in rows if row["safe_name"] == bound.safe_name]
        payload = {"kind": "navigation", "items": rows}
        self._queue_tool_result("list_knowledges", payload)
        return json.dumps(payload, ensure_ascii=False)

    async def _list_files(self, knowledge_name: str = "") -> str:
        knowledge = self._navigation_knowledge(knowledge_name)
        items = await asyncio.to_thread(knowledge.list_files)
        payload = {
            "kind": "navigation",
            "knowledge": knowledge.name,
            "items": items,
        }
        self._queue_tool_result("list_files", payload)
        return json.dumps(payload, ensure_ascii=False)

    async def _list_sections(self, file_name: str, knowledge_name: str = "") -> str:
        knowledge = self._navigation_knowledge(knowledge_name)
        items = await asyncio.to_thread(knowledge.list_sections, file_name)
        payload = {
            "kind": "navigation",
            "knowledge": knowledge.name,
            "file": file_name,
            "items": [self._metadata_only(item) for item in items],
        }
        self._queue_tool_result("list_sections", payload)
        return json.dumps(payload, ensure_ascii=False)

    async def _get_section_metadata(self, section_id: str, knowledge_name: str = "") -> str:
        knowledge = self._navigation_knowledge(knowledge_name)
        section = await asyncio.to_thread(knowledge.get_section, section_id)
        if section is None:
            raise KeyError(
                f"section '{section_id}' does not exist in knowledge '{knowledge.name}'"
            )
        payload = {
            "kind": "navigation",
            "knowledge": knowledge.name,
            "section": section,
        }
        self._queue_tool_result("get_section_metadata", payload)
        return json.dumps(payload, ensure_ascii=False)

    def _build_navigation_tools(self) -> list[FunctionTool]:
        async def list_knowledges() -> str:
            """List knowledges available to this conversation."""
            return await self._list_knowledges()

        async def list_files(knowledge_name: str = "") -> str:
            """List files in a knowledge, or the local conversation's bound knowledge."""
            return await self._list_files(knowledge_name)

        async def list_sections(file_name: str, knowledge_name: str = "") -> str:
            """List section metadata for a file without returning section contents."""
            return await self._list_sections(file_name, knowledge_name)

        async def get_section_metadata(
            section_id: str, knowledge_name: str = ""
        ) -> str:
            """Get metadata for one section without returning its raw content."""
            return await self._get_section_metadata(section_id, knowledge_name)

        return [
            FunctionTool.from_defaults(
                async_fn=list_knowledges,
                name="list_knowledges",
                description=(
                    "Lists knowledges available to this conversation. "
                    "Local conversations see only their bound knowledge."
                ),
            ),
            FunctionTool.from_defaults(
                async_fn=list_files,
                name="list_files",
                description=(
                    "Lists files within a knowledge. Global conversations must provide "
                    "knowledge_name; local conversations use their bound knowledge."
                ),
            ),
            FunctionTool.from_defaults(
                async_fn=list_sections,
                name="list_sections",
                description=(
                    "Lists section metadata within a file. It does not return raw section content."
                ),
            ),
            FunctionTool.from_defaults(
                async_fn=get_section_metadata,
                name="get_section_metadata",
                description=(
                    "Gets one section's metadata, including summary, keywords, conditions, "
                    "definitions, file name, section index, and raw section content."
                ),
            ),
        ]

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
        op_id = self._op_id_var.get()
        if mode == LOCAL_EMBEDDED_RETRIEVAL:
            return await self._embedded.retrieve_local_context(
                knowledge_name=kwargs["knowledge_name"],
                user_query=kwargs["user_query"],
                op_id=op_id,
            )
        if mode == LOCAL_HIERARCHICAL_RETRIEVAL:
            return await self._hierarchical.retrieve_local_context(
                knowledge_name=kwargs["knowledge_name"],
                user_query=kwargs["user_query"],
                op_id=op_id,
            )
        if mode == LOCAL_AGREEMENT_RETRIEVAL:
            return await self._agreement.retrieve_local_context(
                knowledge_name=kwargs["knowledge_name"],
                user_query=kwargs["user_query"],
                op_id=op_id,
            )
        if mode == LOCAL_VECTOR_CONDITIONED_RETRIEVAL:
            return await self._vector_conditioned.retrieve_local_context(
                knowledge_name=kwargs["knowledge_name"],
                user_query=kwargs["user_query"],
                op_id=op_id,
            )
        if mode == GLOBAL_EMBEDDED_RETRIEVAL:
            return await self._embedded.retrieve_global_context(
                user_query=kwargs["user_query"],
                op_id=op_id,
            )
        if mode == GLOBAL_HIERARCHICAL_RETRIEVAL:
            return await self._hierarchical.retrieve_global_context(
                user_query=kwargs["user_query"],
                full_retrieval=kwargs.get("full_retrieval", False),
                op_id=op_id,
            )
        if mode == GLOBAL_AGREEMENT_RETRIEVAL:
            return await self._agreement.retrieve_global_context(
                user_query=kwargs["user_query"],
                full_retrieval=kwargs.get("full_retrieval", False),
                op_id=op_id,
            )
        if mode == GLOBAL_VECTOR_CONDITIONED_RETRIEVAL:
            return await self._vector_conditioned.retrieve_global_context(
                user_query=kwargs["user_query"],
                op_id=op_id,
            )
        raise ValueError(f"unknown retrieval mode: {mode}")

    async def _tool_result(self, mode: str, result) -> str:
        cards = await self._reconstructor.reconstruct(
            result, mode, op_id=self._op_id_var.get()
        )
        self._pending_tool_results.setdefault(mode, deque()).append(
            _tool_result_summary(cards)
        )
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
        self._bus.publish(Event(type=etype, data=data, op_id=self._op_id_var.get() or uuid4().hex))

    # ---- lifecycle ----------------------------------------------------

    async def stream(
        self,
        user_text: str,
        retrieval_mode: str = "auto",
        op_id: str | None = None,
    ) -> str:
        """Run one turn. Emits chat.* events on the bus, returns the final reply."""
        op_id = op_id or uuid4().hex
        token = self._op_id_var.set(op_id)
        try:
            self._pending_tool_results.clear()
            if not self.title_generated:
                await self._generate_title(user_text)
                self.title_generated = True

            agent = self._agent
            if retrieval_mode and retrieval_mode != "auto":
                if retrieval_mode not in RETRIEVAL_TOOLS:
                    raise ValueError(f"unknown retrieval_mode: {retrieval_mode}")
                single = [
                    t
                    for t in self._tools
                    if t.metadata.name == retrieval_mode
                    or t.metadata.name in NON_RETRIEVAL_TOOL_NAMES
                ]
                if not any(t.metadata.name == retrieval_mode for t in single):
                    raise ValueError(
                        f"retrieval tool '{retrieval_mode}' is unavailable for "
                        f"{self.conversation.type} conversations"
                    )
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
                    pending = self._pending_tool_results.get(ev.tool_name)
                    result = None
                    if pending:
                        result = pending.popleft()
                        if not pending:
                            del self._pending_tool_results[ev.tool_name]
                    if ev.tool_output.is_error:
                        result = None
                    self._emit(
                        EventType.CHAT_TOOL_RESULT,
                        {
                            "name": ev.tool_name,
                            "ok": not ev.tool_output.is_error,
                            "result": result,
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
        finally:
            self._op_id_var.reset(token)

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
