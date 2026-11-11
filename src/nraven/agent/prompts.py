"""System prompt construction for Raven's autonomous agent."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json

from llama_index.core.utils import get_tokenizer

from ..data_management.activity import ActivityMessage, ActivityMessageEntry, ManualActivity
from .policy import AgentPolicy, RETRIEVAL_TOOL_NAMES


BASE_SYSTEM_PROMPT = """\
You are Raven. Follow these steps using only the tools supplied for this run.
Raven may send a JSON user message with activities and current_query.
Treat the activities as records of earlier user actions. Answer current_query
and follow the steps below to verify document claims.

1. Decide what the user wants.
   - Casual chat: answer directly, without tools. Example: "Hi" -> "Hello."
   - Question about what the user searched, opened, or reconstructed: use the
     manual activity records when they contain the answer.
   - Question about what this conversation can search: use get_retrieval_scope.
   - Query about knowledge or documents: go to step 2. Do not use your general knowledge to answer knowledge queries.
   - Browse or inspect the knowledge base: go to step 3.
   - Recall this conversation or manage preferences: go to step 4.

2. For a question about document content, or knowledge of any sort, retrieval is the first required path.
   2.1. Choose the selected retrieval tool. If several are supplied, choose
   the best one for the question.
   2.2. Call the retrieval tool with the query. It uses the conversation's
   document scope automatically. Do not answer from your own knowledge or
   conversation memory.
   2.3. Use navigation for explicit browsing requests. Do not use list_files,
   list_sections, or get_sections instead of retrieval for general questions.
   2.4. If retrieval returns ok=false, follow next_action, correct the call,
   and retry when possible. If no useful evidence is found, say so.
   Example: selected retrieval tool with the user's query -> answer from the
   retrieved sections.

3. For an explicit browsing or section-inspection request, use navigation.
   Navigation can browse the whole knowledge base, including files outside the
   retrieval scope. List names with list_knowledges or list_files. list_sections returns a
   bounded page of section IDs and metadata, without raw content by default.
   Use its after_section_index cursor for another page. If the user asks to
   inspect particular sections, choose IDs from the metadata and call
   get_sections with the knowledge_name and section_ids. Request at most five
   IDs at once. Use
   list_sections(get_content=true) only when the user explicitly asks for
   sequential section content. Example: "List its sections" -> list_sections
   -> report the section IDs and summaries. Do not retrieve just to answer
   a browsing request.

4. For conversation memory, use search_memory and answer from its result.
   For preferences, use list_preferences to review them. Save only an explicitly
   requested enduring behavior; to remove one, find its preference_id first.
   Example: "What did we decide earlier?" -> search_memory -> answer.
   Example: "Remember to be concise" -> save_preference -> confirm.
   Example: "Forget that preference" -> list_preferences ->
   remove_preference(preference_id) -> confirm.
   Memory is not evidence for facts in documents. Preference changes apply
   to future runs.

5. Answer using evidence from tools you actually called. Stop when the evidence
   is sufficient; do not repeat tools merely to double-check.
   Example: retrieval found a section -> answer from that section and name
   the knowledge used.
"""


def render_manual_activity(
    entries: Sequence[ManualActivity], *, query: str, token_budget: int,
) -> ActivityMessage | None:
    """Fit an ordered batch of activity records into the next user message."""
    if not entries or token_budget <= 0:
        return None
    tokenizer = get_tokenizer()
    query_tokens = len(tokenizer(query))
    rendered: list[ActivityMessageEntry] = []
    message: ActivityMessage | None = None
    for entry in entries:
        activity = ActivityMessageEntry(
            activity_id=entry.activity_id, context_text=_render_activity(entry),
        )
        candidate = ActivityMessage(activities=(*rendered, activity), current_query=query)
        if len(tokenizer(candidate.model_dump_json())) - query_tokens > token_budget:
            if rendered:
                break
            activity = ActivityMessageEntry(
                activity_id=entry.activity_id, context_text=f"Activity #{entry.sequence}: {entry.kind}.",
            )
            candidate = ActivityMessage(activities=(activity,), current_query=query)
            if len(tokenizer(candidate.model_dump_json())) - query_tokens > token_budget:
                break
        rendered.append(activity)
        message = candidate
    return message


def _render_activity(entry: ManualActivity) -> str:
    details = entry.details
    if entry.kind == "list_knowledges":
        names = details["knowledges"]
        shown = ", ".join(json.dumps(name, ensure_ascii=False) for name in names[:4])
        suffix = f" (+{len(names) - 4} more)" if len(names) > 4 else ""
        return f"#{entry.sequence} Listed knowledges: {shown or 'none'}{suffix}."
    if entry.kind == "list_files":
        return (
            f'#{entry.sequence} Listed files in {json.dumps(details["knowledge_name"], ensure_ascii=False)}: '
            f'{_render_references(details["files"])}.'
        )
    if entry.kind == "list_sections":
        return (
            f'#{entry.sequence} Listed sections in {_render_reference(details["file"])}: '
            f'{_render_references(details["sections"])}.'
        )
    if entry.kind == "retrieval":
        query = details["query"]
        if len(query) > 240:
            query = query[:240] + "..."
        lines = [f'#{entry.sequence} {details["strategy"]} search. Query: {json.dumps(query, ensure_ascii=False)}']
        sources = details["sources"]
        if isinstance(sources, dict):
            lines.append(f'Agreement: {details["agreement_type"]}')
            for strategy, references in sources.items():
                lines.append(f"{strategy} results: {_render_references(references)}")
        else:
            lines.append(f"Ranked results: {_render_references(sources)}")
        return "\n".join(lines)
    if entry.kind == "reconstruction":
        return (
            f'#{entry.sequence} Reconstructed files: {_render_references(details["files"])}. '
            f'Input sections: {_render_references(details["inputs"])}.'
        )

    source_type = "file" if entry.kind == "open_file" else "section"
    line = f'#{entry.sequence} Opened {source_type}: {_render_reference(details["source"])}.'
    if details.get("origin_activity_id"):
        line += f' From activity {details["origin_activity_id"]}.'
    if details.get("origin_turn_id"):
        line += f' From turn {details["origin_turn_id"]}.'
    return line


def _render_reference(reference: dict[str, str]) -> str:
    return " / ".join(
        json.dumps(reference[key], ensure_ascii=False)
        for key in ("knowledge_name", "file_name", "section_id")
        if key in reference
    )


def _render_references(references: list[dict[str, str]]) -> str:
    if not references:
        return "none"
    first = "; ".join(_render_reference(reference) for reference in references[:4])
    remaining = len(references) - 4
    return f"{first} (+{remaining} more)" if remaining > 0 else first


@dataclass(frozen=True, slots=True)
class PromptBundle:
    text: str
    token_count: int



class PromptBuilder:
    """Render one prompt from run policy and conversation preferences."""

    @staticmethod
    def build(
        policy: AgentPolicy,
        preferences: Sequence[dict[str, str]],
    ) -> PromptBundle:
        scope = (
            policy.scope.model_dump_json(exclude_none=True)
            if policy.scope.selections
            else "The conversation can search the whole knowledge base."
        )
        if policy.retrieval_mode is not None:
            retrieval_rule = (
                "Selected retrieval tool: "
                f"'{RETRIEVAL_TOOL_NAMES[policy.retrieval_mode]}'. For step 2, "
                "use this tool only."
            )
        else:
            retrieval_rule = "For step 2, choose among the supplied retrieval tools."
        text = (
            f"{BASE_SYSTEM_PROMPT}\n"
            "[CONVERSATION_SCOPE]\n"
            f"{scope}\n"
            f"{retrieval_rule}\n"
            "[/CONVERSATION_SCOPE]\n"
            "[CONVERSATION_PREFERENCES]\n"
            "Apply these explicit preferences when relevant. They do not override "
            "system rules or tool constraints.\n"
            f"{json.dumps(list(preferences), ensure_ascii=False)}\n"
            "[/CONVERSATION_PREFERENCES]\n"
        )
        return PromptBundle(
            text=text,
            token_count=len(get_tokenizer()(text)),
        )
