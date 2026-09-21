"""System prompt construction for Raven's autonomous agent."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json

from llama_index.core.utils import get_tokenizer

from .policy import AgentPolicy, RETRIEVAL_TOOL_NAMES


BASE_SYSTEM_PROMPT = """\
You are Raven. Follow these steps using only the tools supplied for this run.

1. Decide what the user wants.
   - Casual chat: answer directly, without tools. Example: "Hi" -> "Hello."
   - Facts from documents: go to step 2.
   - Browse or inspect the knowledge base: go to step 3.
   - Recall this conversation or manage preferences: go to step 4.

2. For a question about document content, retrieval is the required path.
   2.1. Choose the selected retrieval tool. If several are supplied, choose
   the best one for the question.
   2.2. A local retrieval tool needs knowledge_name. If it is missing, ask the
   user or use list_knowledges to find a clear match. Never invent a name.
   2.3. Call that retrieval tool. Do not answer from your own knowledge or
   conversation memory.
   2.4. Navigation is secondary: use it only to find a missing retrieval
   argument. Do not use list_files, list_sections, or get_section instead of
   retrieval, even if they seem to contain the answer.
   2.5. If retrieval returns ok=false, follow next_action, correct the call,
   and retry when possible. If no useful evidence is found, say so.
   Example: list_knowledges -> selected local retrieval tool with the found
   knowledge_name and the user's query -> answer from retrieved sections.

3. For an explicit browsing or section-inspection request, use navigation.
   List names with list_knowledges or list_files; list_sections returns IDs by
   default. Use get_section for a requested section. Only when the user asks
   for every section's content or metadata in a file, call list_sections with
   get_content_metadata=true. Example: "List its sections" -> list_sections
   -> report the section IDs. Do not retrieve just to answer a browsing request.

4. For conversation memory, use search_memory and answer from its result.
   For preferences, use list_preferences to review them. Save only an explicitly
   requested enduring behavior; to remove one, find its preference_id first.
   Example: "What did we decide earlier?" -> search_memory -> answer.
   Example: "Remember to be concise" -> save_preference -> confirm.
   Example: "Forget that preference" -> list_preferences ->
   remove_preference(preference_id) -> confirm.
   Memory is not evidence for facts in documents. Preference changes apply
   to future runs.

5. Answer using evidence from tools you actually called. If you inferred a
   knowledge_name, mention your choice in the answer. Stop when the evidence
   is sufficient; do not repeat tools merely to double-check.
   Example: retrieval found a section -> answer from that section and name
   the knowledge used.
"""


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
            f"This is a local conversation bound to knowledge '{policy.knowledge_name}'."
            if policy.conversation_type == "local"
            else (
                "This is a global conversation with no bound knowledge. Local "
                "retrieval and file/section navigation require a knowledge_name."
            )
        )
        if policy.retrieval_mode is not None:
            retrieval_rule = (
                "Selected retrieval tool: "
                f"'{RETRIEVAL_TOOL_NAMES[policy.retrieval_mode]}'. For step 2, "
                "use this tool only."
            )
        elif policy.conversation_type == "local":
            retrieval_rule = (
                "For step 2, choose among the supplied local retrieval tools."
            )
        else:
            retrieval_rule = (
                "For step 2, choose among the supplied local and global retrieval "
                "tools. For a broad question without a knowledge_name, prefer "
                "global retrieval."
            )
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
