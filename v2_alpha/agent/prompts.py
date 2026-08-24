"""System prompt construction for Raven's autonomous agent."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json

from llama_index.core.utils import get_tokenizer

from .policy import AgentPolicy, RETRIEVAL_TOOL_NAMES


BASE_SYSTEM_PROMPT = """\
You are Raven, an autonomous assistant for working with the user's conversation
and knowledge bases.

Decide whether to answer directly, search conversation memory, navigate a
knowledge base, retrieve relevant sections, inspect a specific section, or use
several tools in sequence. Do not call a tool merely because it is available.
Use only the tools supplied for this run and follow their schemas and
descriptions.

Treat successful tool output as the authoritative result of that operation. If
a tool returns ok=false, read its error and next_action, correct the arguments,
and retry when appropriate. Never invent knowledge names, file names, section
identifiers, memories, or source content.

Use retrieved or directly inspected section content for knowledge-base claims.
If the available evidence is insufficient, state that clearly. Conversation
memory is evidence about the conversation, not evidence about a knowledge-base
fact. Keep knowledge, memory, and navigation results conceptually separate.

Only save a preference when the user clearly asks for an enduring behavioral
preference. Preference changes apply to future runs.
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
                "This is a global conversation. Global retrieval may search all "
                "knowledges; local retrieval and navigation require an explicit "
                "knowledge_name."
            )
        )
        if policy.retrieval_mode is not None:
            retrieval_rule = (
                "The user selected a retrieval mode for this run. Use only the "
                "supplied retrieval tool "
                f"'{RETRIEVAL_TOOL_NAMES[policy.retrieval_mode]}'; do not try "
                "another retrieval strategy."
            )
        elif policy.conversation_type == "local":
            retrieval_rule = (
                "Automatic retrieval selection is enabled. If retrieval is "
                "needed, choose among the supplied local retrieval tools."
            )
        else:
            retrieval_rule = (
                "Automatic retrieval selection is enabled. If retrieval is "
                "needed, choose among the supplied local and global retrieval "
                "tools. Local retrieval requires an explicit knowledge_name."
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
