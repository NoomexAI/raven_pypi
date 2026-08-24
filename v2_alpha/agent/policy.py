"""Hard scope and retrieval constraints for agent runs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..core.errors import ErrorCode, RavenError


class RetrievalMode(StrEnum):
    LOCAL_EMBEDDED = "local_embedded"
    LOCAL_HIERARCHICAL = "local_hierarchical"
    LOCAL_AGREEMENT = "local_agreement"
    LOCAL_VECTOR_CONDITIONED = "local_vector_conditioned"
    GLOBAL_EMBEDDED = "global_embedded"
    GLOBAL_HIERARCHICAL = "global_hierarchical"
    GLOBAL_AGREEMENT = "global_agreement"
    GLOBAL_VECTOR_CONDITIONED = "global_vector_conditioned"


LOCAL_RETRIEVAL_MODES = frozenset(
    {
        RetrievalMode.LOCAL_EMBEDDED,
        RetrievalMode.LOCAL_HIERARCHICAL,
        RetrievalMode.LOCAL_AGREEMENT,
        RetrievalMode.LOCAL_VECTOR_CONDITIONED,
    }
)

GLOBAL_RETRIEVAL_MODES = frozenset(
    {
        RetrievalMode.GLOBAL_EMBEDDED,
        RetrievalMode.GLOBAL_HIERARCHICAL,
        RetrievalMode.GLOBAL_AGREEMENT,
        RetrievalMode.GLOBAL_VECTOR_CONDITIONED,
    }
    | LOCAL_RETRIEVAL_MODES
)

RETRIEVAL_TOOL_NAMES = {
    mode: f"{mode.value}_retrieval"
    for mode in RetrievalMode
}



@dataclass(frozen=True, slots=True)
class AgentPolicy:
    """Deterministic restrictions without classifying the user's query."""

    conversation_type: str
    knowledge_name: str | None
    retrieval_mode: RetrievalMode | None
    max_iterations: int
    top_k: int


    @classmethod
    def create(
        cls,
        conversation: Any,
        retrieval_mode: RetrievalMode | str | None,
        *,
        max_iterations: int,
        top_k: int,
    ) -> "AgentPolicy":
        conversation_type = conversation.type
        knowledge_name = conversation.knowledge_name
        if conversation_type not in {"local", "global"}:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                f"Unsupported conversation type '{conversation_type}'.",
            )
        if conversation_type == "local" and not knowledge_name:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "A local conversation requires a knowledge_name.",
            )
        if max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        if top_k <= 0:
            raise ValueError("top_k must be positive")

        selected = cls._parse_mode(retrieval_mode)
        allowed_modes = cls._scope_modes(conversation_type)
        if selected is not None and selected not in allowed_modes:
            raise RavenError(
                ErrorCode.INVALID_RETRIEVAL_MODE,
                f"Retrieval mode '{selected.value}' is not valid for a {conversation_type} conversation.",
            )
        return cls(
            conversation_type=conversation_type,
            knowledge_name=knowledge_name,
            retrieval_mode=selected,
            max_iterations=max_iterations,
            top_k=top_k,
        )


    @property
    def scope_modes(self) -> frozenset[RetrievalMode]:
        return self._scope_modes(self.conversation_type)


    @property
    def retrieval_tool_names(self) -> frozenset[str]:
        return frozenset(RETRIEVAL_TOOL_NAMES[mode] for mode in self.scope_modes)


    def permits(self, mode: RetrievalMode) -> bool:
        return mode in self.scope_modes and (
            self.retrieval_mode is None or mode == self.retrieval_mode
        )


    @staticmethod
    def _scope_modes(conversation_type: str) -> frozenset[RetrievalMode]:
        return (
            LOCAL_RETRIEVAL_MODES
            if conversation_type == "local"
            else GLOBAL_RETRIEVAL_MODES
        )


    @staticmethod
    def _parse_mode(value: RetrievalMode | str | None) -> RetrievalMode | None:
        if value is None or value == "auto":
            return None
        try:
            return value if isinstance(value, RetrievalMode) else RetrievalMode(value)
        except ValueError as exc:
            raise RavenError(
                ErrorCode.INVALID_RETRIEVAL_MODE,
                f"Unknown retrieval mode '{value}'.",
            ) from exc
