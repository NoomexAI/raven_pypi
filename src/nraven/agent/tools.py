"""Scope-aware tools exposed to Raven's autonomous agent."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from llama_index.core.tools import FunctionTool

from ..core.errors import ErrorCode, RavenError, error_payload
from ..core.operations import Operation
from ..core.retrieval_scope import RetrievalScope
from ..data_management.conversation_manager import Conversation
from ..data_management.knowledge_base import Knowledge, KnowledgeBase
from .contracts import RetrievalPipelines
from .policy import (
    RETRIEVAL_TOOL_NAMES,
    AgentPolicy,
    RetrievalMode,
)


RETRIEVAL_DESCRIPTIONS = {
    RetrievalMode.EMBEDDED: (
        "Retrieve sections by embedding similarity within the conversation scope. "
        "Use this for specific facts, exact details, definitions, numbers, and requirements."
    ),
    RetrievalMode.HIERARCHICAL: (
        "Rank section metadata with an LLM within the conversation scope. Use for narrative context, "
        "chronology, themes, and relationships. Prefer vector-conditioned retrieval when it can "
        "answer the question with less work."
    ),
    RetrievalMode.AGREEMENT: (
        "Compare embedded and hierarchical retrieval within the conversation scope. "
        "Use when independent searches should corroborate critical evidence. This is the "
        "most expensive strategy."
    ),
    RetrievalMode.VECTOR_CONDITIONED: (
        "Shortlist files by vector similarity within the conversation scope and rank their "
        "sections with an LLM. Use this by default for a balance of factual relevance, "
        "document context, and compute cost."
    ),
}

RECOVERABLE_ERRORS = frozenset(
    {
        ErrorCode.INVALID_METADATA,
        ErrorCode.INVALID_KNOWLEDGE_NAME,
        ErrorCode.KNOWLEDGE_NOT_FOUND,
        ErrorCode.FILE_NOT_FOUND,
        ErrorCode.FILE_ID_NAME_MISMATCH,
        ErrorCode.SECTION_NOT_FOUND,
        ErrorCode.INVALID_RETRIEVAL_MODE,
        ErrorCode.RETRIEVAL_MODE_NOT_ALLOWED,
        ErrorCode.INVALID_PREFERENCE_ID,
        ErrorCode.PREFERENCE_NOT_FOUND,
    }
)

SECTION_PAGE_SIZE = 20
CONTENT_SECTION_PAGE_SIZE = 3
MAX_SECTION_PAGE_SIZE = 50
MAX_CONTENT_SECTION_PAGE_SIZE = 5
MAX_SECTION_FETCH = 5


@dataclass(frozen=True, slots=True)
class ToolResult:
    """One tool response containing model content and bounded UI metadata."""

    ok: bool
    result: Any = None
    ui_summary: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, str]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    next_action: str | None = None


    def to_model_text(self) -> str:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "result": self.result,
            "ui_summary": self.ui_summary,
            "evidence": self.evidence,
        }
        if self.error is not None:
            payload["error"] = self.error
        if self.next_action is not None:
            payload["next_action"] = self.next_action
        return json.dumps(payload, ensure_ascii=False)



class ToolBuilder:
    """Build retrieval, navigation, memory, and preference tools for one run."""

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        retrieval_pipelines: RetrievalPipelines,
    ) -> None:
        self._knowledge_base = knowledge_base
        self._retrieval_pipelines = retrieval_pipelines


    def build(
        self,
        conversation: Conversation,
        policy: AgentPolicy,
        operation: Operation,
        *,
        on_preferences_changed: Callable[[str], None],
    ) -> list[FunctionTool]:
        tools = [
            self._retrieval_tool(mode, policy, operation)
            for mode in RetrievalMode
            if policy.permits(mode)
        ]
        tools.extend(self._navigation_tools(policy.scope, operation))
        tools.extend(
            self._memory_tools(
                conversation,
                operation,
                on_preferences_changed,
            )
        )
        return tools


    def _retrieval_tool(
        self,
        mode: RetrievalMode,
        policy: AgentPolicy,
        operation: Operation,
    ) -> FunctionTool:
        async def retrieve(query: str) -> str:
            return await self._run_retrieval(mode, query, policy, operation)

        return FunctionTool.from_defaults(
            async_fn=retrieve,
            name=RETRIEVAL_TOOL_NAMES[mode],
            description=RETRIEVAL_DESCRIPTIONS[mode],
        )


    async def _run_retrieval(
        self,
        mode: RetrievalMode,
        query: str,
        policy: AgentPolicy,
        operation: Operation,
    ) -> str:
        tool_name = RETRIEVAL_TOOL_NAMES[mode]
        try:
            operation.raise_if_cancelled()
            if not isinstance(query, str) or not query.strip():
                raise RavenError(
                    ErrorCode.INVALID_METADATA,
                    "Retrieval query must be a non-empty string.",
                )
            if not policy.permits(mode):
                return ToolResult(
                    ok=False,
                    error={
                        "code": ErrorCode.RETRIEVAL_MODE_NOT_ALLOWED.value,
                        "message": "This retrieval mode is not allowed for the current run.",
                    },
                    ui_summary={"kind": "tool_error", "tool": tool_name},
                    next_action=self._retrieval_instruction(policy),
                ).to_model_text()

            pipeline = getattr(self._retrieval_pipelines, mode.value)
            limit_field = {
                RetrievalMode.EMBEDDED: "top_k",
                RetrievalMode.HIERARCHICAL: "top_k_section",
                RetrievalMode.AGREEMENT: "top_k",
                RetrievalMode.VECTOR_CONDITIONED: "top_k_sections",
            }[mode]
            config = policy.runtime_config
            options: dict[str, int | bool] = {limit_field: config.retrieval_top_k}
            if mode is not RetrievalMode.EMBEDDED:
                options["max_per_read"] = config.retrieval_max_per_read
                options["anchor_count"] = config.retrieval_anchor_count
            if mode in (RetrievalMode.HIERARCHICAL, RetrievalMode.AGREEMENT):
                options["top_k_knowledge"] = config.retrieval_top_k_knowledge
                options["full_retrieval"] = config.full_retrieval
            if mode is RetrievalMode.VECTOR_CONDITIONED:
                options["top_k_files"] = config.retrieval_top_k_files
            retrieval_task = await pipeline.retrieve(
                query,
                scope=policy.scope,
                operation=operation,
                **options,
            )
            result = await retrieval_task.result()

            evidence = section_references(result)
            return ToolResult(
                ok=True,
                result=result,
                evidence=evidence,
                ui_summary={
                    "kind": "retrieval",
                    "section_count": len(evidence),
                    "sections": evidence,
                },
            ).to_model_text()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._recoverable_result(
                tool_name,
                exc,
                self._retrieval_instruction(policy),
            )


    def _navigation_tools(
        self,
        scope: RetrievalScope,
        operation: Operation,
    ) -> list[FunctionTool]:
        return [
            self._list_knowledges_tool(scope, operation),
            self._list_files_tool(scope, operation),
            self._list_sections_tool(scope, operation),
            self._get_sections_tool(scope, operation),
        ]


    def _list_knowledges_tool(
        self,
        scope: RetrievalScope,
        operation: Operation,
    ) -> FunctionTool:
        async def list_knowledges() -> str:
            try:
                operation.raise_if_cancelled()
                if scope.selections:
                    resolved = await self._knowledge_base.resolve_scope(scope)
                    knowledges = [
                        {"name": name, "safe_name": name, "file_count": len(file_ids)}
                        for name, file_ids in resolved.items()
                    ]
                else:
                    knowledges = await self._knowledge_base.list()
                return ToolResult(
                    ok=True,
                    result=knowledges,
                    ui_summary={
                        "kind": "navigation",
                        "operation": "list_knowledges",
                        "knowledge_names": [item.get("safe_name") for item in knowledges],
                    },
                ).to_model_text()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return self._recoverable_result("list_knowledges", exc, "Retry listing knowledges.")

        return FunctionTool.from_defaults(
            async_fn=list_knowledges,
            name="list_knowledges",
            description="List the knowledges available in the conversation scope.",
        )


    def _list_files_tool(self, scope: RetrievalScope, operation: Operation) -> FunctionTool:
        async def list_files(knowledge_name: str) -> str:
            return await self._list_files(knowledge_name, operation, scope=scope)

        return FunctionTool.from_defaults(
            async_fn=list_files,
            name="list_files",
            description="List the files in the named knowledge that are in the conversation scope.",
        )


    async def _list_files(
        self,
        knowledge_name: str,
        operation: Operation,
        *,
        scope: RetrievalScope = RetrievalScope(),
    ) -> str:
        try:
            operation.raise_if_cancelled()
            knowledge = self._knowledge(knowledge_name)
            eligible_ids = await self._navigation_file_ids(scope, knowledge)
            files = await asyncio.to_thread(knowledge.list_files)
            if eligible_ids is not None:
                files = [file for file in files if file["file_id"] in eligible_ids]
            return ToolResult(
                ok=True,
                result=files,
                ui_summary={
                    "kind": "navigation",
                    "operation": "list_files",
                    "knowledge_name": knowledge_name,
                    "file_names": [item.get("file_name") for item in files],
                },
            ).to_model_text()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._recoverable_result(
                "list_files",
                exc,
                "Check the knowledge_name and retry.",
            )


    def _list_sections_tool(self, scope: RetrievalScope, operation: Operation) -> FunctionTool:
        async def list_sections(
            knowledge_name: str,
            file_name: str,
            get_content: bool = False,
            after_section_index: int = 0,
            limit: int | None = None,
        ) -> str:
            return await self._list_sections(
                knowledge_name,
                file_name,
                operation,
                scope=scope,
                get_content=get_content,
                after_section_index=after_section_index,
                limit=limit,
            )

        return FunctionTool.from_defaults(
            async_fn=list_sections,
            name="list_sections",
            description=(
                "List section IDs and metadata in a named file within the conversation scope. "
                "The default page contains up to 20 sections. Pass after_section_index from "
                "the previous result to continue. The limit is at most 50, or 5 with "
                "get_content=true. Use get_content only for explicit inspection."
            ),
        )


    async def _list_sections(
        self,
        knowledge_name: str,
        file_name: str,
        operation: Operation,
        *,
        scope: RetrievalScope = RetrievalScope(),
        get_content: bool = False,
        after_section_index: int = 0,
        limit: int | None = None,
    ) -> str:
        try:
            operation.raise_if_cancelled()
            if type(get_content) is not bool:
                raise RavenError(ErrorCode.INVALID_METADATA, "get_content must be a boolean.")
            if type(after_section_index) is not int or after_section_index < 0:
                raise RavenError(ErrorCode.INVALID_METADATA, "after_section_index must be a non-negative integer.")
            maximum = MAX_CONTENT_SECTION_PAGE_SIZE if get_content else MAX_SECTION_PAGE_SIZE
            page_size = limit if limit is not None else (
                CONTENT_SECTION_PAGE_SIZE if get_content else SECTION_PAGE_SIZE
            )
            if type(page_size) is not int or not 1 <= page_size <= maximum:
                raise RavenError(
                    ErrorCode.INVALID_METADATA,
                    f"limit must be an integer between 1 and {maximum} for this view.",
                )
            knowledge = self._knowledge(knowledge_name)
            eligible_ids = await self._navigation_file_ids(scope, knowledge)
            if eligible_ids is not None:
                files = await asyncio.to_thread(knowledge.list_files)
                if not any(file["file_name"] == file_name and file["file_id"] in eligible_ids for file in files):
                    raise RavenError(ErrorCode.FILE_NOT_FOUND, f"File '{file_name}' is outside the conversation scope.")
            page = await asyncio.to_thread(
                knowledge.list_sections_page,
                file_name,
                page_size + 1,
                after_section_index,
            )
            if eligible_ids is not None and any(section["file_id"] not in eligible_ids for section in page):
                raise RavenError(ErrorCode.FILE_NOT_FOUND, f"File '{file_name}' is outside the conversation scope.")
            if not page and not await asyncio.to_thread(knowledge.file_exists, file_name):
                raise RavenError(
                    ErrorCode.FILE_NOT_FOUND,
                    f"File '{file_name}' does not exist in knowledge '{knowledge_name}'.",
                )
            has_more = len(page) > page_size
            sections = page[:page_size]
            section_ids = [section["section_id"] for section in sections]
            result = [
                {
                    key: section.get(key)
                    for key in (
                        "section_id", "section_index", "summary", "keywords",
                        "conditions", "definitions", "source_range",
                    )
                }
                | ({"raw_content": section.get("raw_content", "")} if get_content else {})
                for section in sections
            ]
            next_index = sections[-1]["section_index"] if has_more else None
            evidence = section_references(
                [{"knowledge_name": knowledge_name, **section} for section in sections]
            ) if get_content else []
            return ToolResult(
                ok=True,
                result=result,
                evidence=evidence,
                ui_summary={
                    "kind": "navigation",
                    "operation": "list_sections",
                    "knowledge_name": knowledge_name,
                    "file_name": file_name,
                    "section_ids": section_ids,
                    "has_more": has_more,
                    "next_after_section_index": next_index,
                },
            ).to_model_text()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._recoverable_result(
                "list_sections",
                exc,
                "Check the knowledge_name and file_name, then retry.",
            )


    def _get_sections_tool(self, scope: RetrievalScope, operation: Operation) -> FunctionTool:
        async def get_sections(knowledge_name: str, section_ids: list[str]) -> str:
            return await self._get_sections(knowledge_name, section_ids, operation, scope=scope)

        return FunctionTool.from_defaults(
            async_fn=get_sections,
            name="get_sections",
            description=(
                "Get metadata and raw content for 1 to 5 selected section IDs in a "
                "named knowledge within the conversation scope. Returns them in requested order."
            ),
        )


    async def _get_sections(
        self,
        knowledge_name: str,
        section_ids: list[str],
        operation: Operation,
        *,
        scope: RetrievalScope = RetrievalScope(),
    ) -> str:
        try:
            operation.raise_if_cancelled()
            if (
                not isinstance(section_ids, list)
                or not 1 <= len(section_ids) <= MAX_SECTION_FETCH
                or any(not isinstance(section_id, str) or not section_id.strip() for section_id in section_ids)
            ):
                raise RavenError(
                    ErrorCode.INVALID_METADATA,
                    f"section_ids must contain 1 to {MAX_SECTION_FETCH} non-empty strings.",
                )
            knowledge = self._knowledge(knowledge_name)
            eligible_ids = await self._navigation_file_ids(scope, knowledge)
            result: list[dict[str, Any]] = []
            for section_id in section_ids:
                operation.raise_if_cancelled()
                section = await asyncio.to_thread(knowledge.get_section, section_id)
                if section is None or (eligible_ids is not None and section["file_id"] not in eligible_ids):
                    raise RavenError(
                        ErrorCode.SECTION_NOT_FOUND,
                        f"Section '{section_id}' does not exist in knowledge '{knowledge_name}'.",
                    )
                result.append({"knowledge_name": knowledge_name, **section})
            evidence = section_references(result)
            return ToolResult(
                ok=True,
                result=result,
                evidence=evidence,
                ui_summary={
                    "kind": "section",
                    "sections": evidence,
                },
            ).to_model_text()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._recoverable_result(
                "get_sections",
                exc,
                "Check the knowledge_name and section_ids from list_sections, then retry.",
            )


    def _memory_tools(
        self,
        conversation: Conversation,
        operation: Operation,
        on_preferences_changed: Callable[[str], None],
    ) -> list[FunctionTool]:
        async def search_memory(query: str) -> str:
            try:
                operation.raise_if_cancelled()
                messages = await conversation.search_memory(query)
                result = [message_record(message) for message in messages]
                return ToolResult(
                    ok=True,
                    result=result,
                    ui_summary={"kind": "memory", "message_count": len(result)},
                ).to_model_text()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return self._recoverable_result(
                    "search_memory",
                    exc,
                    "Provide a non-empty memory query and retry.",
                )

        async def list_preferences() -> str:
            try:
                operation.raise_if_cancelled()
                preferences = conversation.get_preferences()
                return ToolResult(
                    ok=True,
                    result=preferences,
                    ui_summary={"kind": "preferences", "count": len(preferences)},
                ).to_model_text()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return self._recoverable_result(
                    "list_preferences",
                    exc,
                    "Retry listing preferences.",
                )

        async def save_preference(text: str) -> str:
            try:
                operation.raise_if_cancelled()
                preference_task = await conversation.save_preference(
                    text,
                    operation=operation,
                )
                preference = await preference_task.result()
                on_preferences_changed(conversation.conversation_id)
                return ToolResult(
                    ok=True,
                    result=preference,
                    ui_summary={
                        "kind": "preference",
                        "changed": True,
                        "preference_id": preference["preference_id"],
                    },
                    next_action="The preference will apply to the next agent run.",
                ).to_model_text()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return self._recoverable_result(
                    "save_preference",
                    exc,
                    "Provide a concise non-empty preference and retry.",
                )

        async def remove_preference(preference_id: str) -> str:
            try:
                operation.raise_if_cancelled()
                preference_task = await conversation.remove_preference(
                    preference_id,
                    operation=operation,
                )
                preference = await preference_task.result()
                on_preferences_changed(conversation.conversation_id)
                return ToolResult(
                    ok=True,
                    result=preference,
                    ui_summary={
                        "kind": "preference",
                        "changed": True,
                        "preference_id": preference["preference_id"],
                    },
                    next_action="The preference removal will apply to the next agent run.",
                ).to_model_text()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return self._recoverable_result(
                    "remove_preference",
                    exc,
                    "List preferences, choose a valid preference_id, and retry.",
                )

        return [
            FunctionTool.from_defaults(
                async_fn=search_memory,
                name="search_memory",
                description=(
                    "Search semantically relevant messages from this conversation's "
                    "memory. Use this when the user asks about prior discussion, "
                    "past decisions, or information previously shared in this "
                    "conversation."
                ),
            ),
            FunctionTool.from_defaults(
                async_fn=list_preferences,
                name="list_preferences",
                description=(
                    "List explicit behavioral preferences saved for this conversation. "
                    "Use this before modifying or removing a preference when you "
                    "need to identify the correct preference_id."
                ),
            ),
            FunctionTool.from_defaults(
                async_fn=save_preference,
                name="save_preference",
                description=(
                    "Save an explicit enduring user preference for future runs. Use "
                    "this only when the user clearly asks Raven to remember a "
                    "behavioral preference, not for ordinary instructions."
                ),
            ),
            FunctionTool.from_defaults(
                async_fn=remove_preference,
                name="remove_preference",
                description=(
                    "Remove a preference by its exact preference_id. Use this only "
                    "when the user explicitly asks to forget or remove a saved "
                    "preference; list preferences first if the ID is unknown."
                ),
            ),
        ]


    async def _navigation_file_ids(
        self, scope: RetrievalScope, knowledge: Knowledge
    ) -> frozenset[str] | None:
        if not scope.selections:
            return None
        resolved = await self._knowledge_base.resolve_scope(scope)
        if knowledge.name not in resolved:
            raise RavenError(
                ErrorCode.KNOWLEDGE_NOT_FOUND,
                f"Knowledge '{knowledge.name}' is outside the conversation scope.",
            )
        return resolved[knowledge.name]


    def _knowledge(self, knowledge_name: str) -> Knowledge:
        if not isinstance(knowledge_name, str) or not knowledge_name.strip():
            raise RavenError(
                ErrorCode.INVALID_KNOWLEDGE_NAME,
                "knowledge_name must be a non-empty string.",
            )
        return self._knowledge_base.get(knowledge_name)


    @staticmethod
    def _recoverable_result(
        tool_name: str,
        error: BaseException,
        next_action: str,
    ) -> str:
        if isinstance(error, RavenError):
            if error.code not in RECOVERABLE_ERRORS:
                raise error
        elif not isinstance(error, (TypeError, ValueError, KeyError)):
            raise error
        return ToolResult(
            ok=False,
            error=error_payload(error),
            ui_summary={"kind": "tool_error", "tool": tool_name},
            next_action=next_action,
        ).to_model_text()


    @staticmethod
    def _retrieval_instruction(policy: AgentPolicy) -> str:
        if policy.retrieval_mode is not None:
            return f"Use {RETRIEVAL_TOOL_NAMES[policy.retrieval_mode]}."
        return "Choose one of the supplied retrieval tools."


def section_references(value: Any) -> list[dict[str, str]]:
    """Extract and deduplicate source references from a tool result."""
    references: dict[tuple[str, str, str], dict[str, str]] = {}

    def visit(item: Any) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            return

        retrieved_content = item.get("retrieved_content")
        if retrieved_content is not None:
            visit(retrieved_content)
            return
        if not all(key in item for key in ("knowledge_name", "file_name", "section_id")):
            for child in item.values():
                visit(child)
            return

        knowledge_name = item.get("knowledge_name")
        file_name = item.get("file_name")
        section_id = item.get("section_id")
        if (
            isinstance(knowledge_name, str)
            and knowledge_name
            and isinstance(file_name, str)
            and file_name
            and isinstance(section_id, str)
            and section_id
        ):
            key = (knowledge_name, file_name, section_id)
            references[key] = {
                "knowledge_name": knowledge_name,
                "file_name": file_name,
                "section_id": section_id,
            }

    visit(value)
    return list(references.values())


def message_record(message: Any) -> dict[str, str]:
    role = getattr(getattr(message, "role", None), "value", None)
    return {
        "role": role if isinstance(role, str) else str(getattr(message, "role", "unknown")),
        "content": str(getattr(message, "content", "") or ""),
    }
