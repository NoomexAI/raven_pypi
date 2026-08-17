"""Tool providers exposed to the autonomous RAVEN agent."""

from __future__ import annotations

import json
from typing import Any, Callable

from llama_index.core.tools import FunctionTool

from ..conversation_session import ConversationSession
from ..events import Event, EventBus, EventType
from ..pipeline import GLOBAL_AGREEMENT_RETRIEVAL, GLOBAL_HIERARCHICAL_RETRIEVAL
from .catalog import (
    GLOBAL_TOOL_NAMES,
    LOCAL_TOOL_NAMES,
    MEMORY_TOOL_NAMES,
    NAVIGATION_TOOL_NAMES,
    RETRIEVAL_TOOLS,
    TOOL_DESCRIPTIONS,
    summarize_reconstructed_evidence,
)
from .context import AgentRunContext
from .policy import AgentPolicy


def _tool(name: str, fn: Callable[..., Any], description: str) -> FunctionTool:
    return FunctionTool.from_defaults(async_fn=fn, name=name, description=description)


def _record(run: AgentRunContext | None, name: str, payload: dict[str, Any], evidence: str | None = None) -> None:
    if run is not None:
        run.record_tool_result(name, payload, evidence=evidence)


def _recoverable_error(
    run: AgentRunContext | None,
    tool_name: str,
    exc: Exception,
    next_action: str,
) -> str:
    """Return a model-readable tool error for invalid/recoverable requests."""
    payload = {
        "ok": False,
        "error": {
            "code": "invalid_tool_request",
            "message": str(exc),
        },
        "next_action": next_action,
    }
    _record(run, tool_name, payload)
    return json.dumps(payload, ensure_ascii=False)


class MemoryToolProvider:
    def build(self, session: ConversationSession, run: AgentRunContext | None) -> dict[str, FunctionTool]:
        async def get_memory(query: str) -> str:
            try:
                payload = await session.search_memory(query)
            except (KeyError, ValueError) as exc:
                return _recoverable_error(
                    run,
                    "get_memory",
                    exc,
                    "Provide a non-empty memory query and retry, or answer without memory if it is not needed.",
                )
            _record(run, "get_memory", payload, "memory")
            return json.dumps(payload, ensure_ascii=False)

        async def save_preference(preference: str) -> str:
            try:
                payload = await session.save_preference(preference)
            except (KeyError, ValueError) as exc:
                return _recoverable_error(
                    run,
                    "save_preference",
                    exc,
                    "Provide the preference as a concise non-empty statement and retry.",
                )
            _record(run, "save_preference", payload)
            return json.dumps(payload, ensure_ascii=False)

        return {
            "get_memory": _tool("get_memory", get_memory, "Search relevant messages from this conversation's past memory."),
            "save_preference": _tool("save_preference", save_preference, "Save an explicit user preference for future turns."),
        }


class NavigationToolProvider:
    def build(self, session: ConversationSession, run: AgentRunContext | None) -> dict[str, FunctionTool]:
        async def list_knowledges() -> str:
            try:
                payload = await session.list_knowledges()
            except (KeyError, ValueError) as exc:
                return _recoverable_error(
                    run,
                    "list_knowledges",
                    exc,
                    "Retry without arguments. If the failure persists, choose another available tool.",
                )
            _record(run, "list_knowledges", payload, "navigation")
            return json.dumps(payload, ensure_ascii=False)

        async def list_files(knowledge_name: str = "") -> str:
            try:
                payload = await session.list_files(knowledge_name)
            except (KeyError, ValueError) as exc:
                return _recoverable_error(
                    run,
                    "list_files",
                    exc,
                    "For a global conversation, provide an explicit knowledge_name, then retry. For a local conversation, use its bound knowledge.",
                )
            _record(run, "list_files", payload, "navigation")
            return json.dumps(payload, ensure_ascii=False)

        async def list_sections(file_name: str, knowledge_name: str = "") -> str:
            try:
                payload = await session.list_sections(file_name, knowledge_name)
            except (KeyError, ValueError) as exc:
                return _recoverable_error(
                    run,
                    "list_sections",
                    exc,
                    "Verify the file name with list_files, provide knowledge_name for global navigation, and retry.",
                )
            _record(run, "list_sections", payload, "navigation")
            return json.dumps(payload, ensure_ascii=False)

        async def get_section_metadata(section_id: str, knowledge_name: str = "") -> str:
            try:
                payload = await session.get_section_metadata(section_id, knowledge_name)
            except (KeyError, ValueError) as exc:
                return _recoverable_error(
                    run,
                    "get_section_metadata",
                    exc,
                    "Verify the section_id using list_sections, provide knowledge_name for global navigation, and retry.",
                )
            _record(run, "get_section_metadata", payload, "knowledge")
            return json.dumps(payload, ensure_ascii=False)

        return {
            "list_knowledges": _tool("list_knowledges", list_knowledges, "List knowledges available to this conversation."),
            "list_files": _tool("list_files", list_files, "List files within a knowledge."),
            "list_sections": _tool("list_sections", list_sections, "List section metadata within a file without raw content."),
            "get_section_metadata": _tool("get_section_metadata", get_section_metadata, "Read one section's metadata and raw content."),
        }


class RetrievalToolProvider:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def build(
        self,
        session: ConversationSession,
        run: AgentRunContext | None,
        allowed: frozenset[str],
    ) -> dict[str, FunctionTool]:
        tools: dict[str, FunctionTool] = {}
        for mode in sorted(RETRIEVAL_TOOLS):
            if mode not in allowed:
                continue
            local = mode in LOCAL_TOOL_NAMES
            full_retrieval = mode in {GLOBAL_HIERARCHICAL_RETRIEVAL, GLOBAL_AGREEMENT_RETRIEVAL}

            def make_retrieval_function(mode: str, local: bool, full_retrieval: bool):
                async def execute(user_query: str, knowledge_name: str = "", full: bool = False) -> str:
                    target = session.conversation.knowledge_name or knowledge_name.strip()
                    if local and not target:
                        return _recoverable_error(
                            run,
                            mode,
                            ValueError("knowledge_name is required for local retrieval"),
                            "Provide the bound knowledge_name and retry this local retrieval tool.",
                        )
                    operation_id = run.operation_id if run else ""
                    try:
                        result = await session.retrieve(
                            mode,
                            user_query=user_query,
                            knowledge_name=target,
                            full_retrieval=full if full_retrieval else False,
                            op_id=operation_id,
                        )
                        cards = await session.reconstruct(result, mode, operation_id)
                    except (KeyError, ValueError) as exc:
                        return _recoverable_error(
                            run,
                            mode,
                            exc,
                            "Check the knowledge scope and query arguments, then retry or choose another permitted retrieval/navigation tool.",
                        )
                    _record(
                        run,
                        mode,
                        summarize_reconstructed_evidence(cards),
                        "knowledge" if result else None,
                    )
                    if cards and run is not None:
                        for card in cards:
                            for section in card.get("sections", []):
                                if section.get("highlighted"):
                                    self._bus.publish(Event(
                                        type=EventType.CHAT_RETRIEVED,
                                        data={
                                            "knowledge": card.get("knowledge_name"),
                                            "file": card.get("file_name"),
                                            "section_id": section.get("section_id"),
                                        },
                                        op_id=operation_id,
                                    ))
                    return json.dumps(result, ensure_ascii=False)

                if local:
                    async def local_retrieve(user_query: str, knowledge_name: str = "") -> str:
                        return await execute(user_query, knowledge_name)
                    return local_retrieve
                if full_retrieval:
                    async def global_full_retrieve(user_query: str, full_retrieval: bool = False) -> str:
                        return await execute(user_query, full=full_retrieval)
                    return global_full_retrieve
                async def global_retrieve(user_query: str) -> str:
                    return await execute(user_query)
                return global_retrieve

            tools[mode] = _tool(
                mode,
                make_retrieval_function(mode, local, full_retrieval),
                TOOL_DESCRIPTIONS[mode],
            )
        return tools


def build_tools(
    session: ConversationSession,
    run: AgentRunContext | None,
    policy: AgentPolicy,
    bus: EventBus,
) -> list[FunctionTool]:
    allowed = policy.allowed_tools(session.conversation.type)
    tools: dict[str, FunctionTool] = {}
    tools.update(MemoryToolProvider().build(session, run))
    tools.update(NavigationToolProvider().build(session, run))
    tools.update(RetrievalToolProvider(bus).build(session, run, allowed))
    return [tools[name] for name in sorted(tools) if name in allowed]
