"""Hard scope and retrieval constraints for agent runs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..core.config import RuntimeConfig
from ..core.errors import ErrorCode, RavenError
from ..core.retrieval_scope import RetrievalScope


class RetrievalMode(StrEnum):
    EMBEDDED = "embedded"
    HIERARCHICAL = "hierarchical"
    AGREEMENT = "agreement"
    VECTOR_CONDITIONED = "vector_conditioned"

RETRIEVAL_TOOL_NAMES = {
    mode: f"{mode.value}_retrieval"
    for mode in RetrievalMode
}



@dataclass(frozen=True, slots=True)
class AgentPolicy:
    """Deterministic restrictions without classifying the user's query."""

    scope: RetrievalScope
    retrieval_mode: RetrievalMode | None
    max_iterations: int
    runtime_config: RuntimeConfig


    @classmethod
    def create(
        cls,
        conversation: Any,
        retrieval_mode: RetrievalMode | str | None,
        *,
        max_iterations: int,
        runtime_config: RuntimeConfig,
    ) -> "AgentPolicy":
        if max_iterations <= 0:
            raise ValueError("max_iterations must be positive")

        return cls(
            scope=conversation.scope,
            retrieval_mode=cls.parse_mode(retrieval_mode),
            max_iterations=max_iterations,
            runtime_config=runtime_config,
        )


    @property
    def retrieval_tool_names(self) -> frozenset[str]:
        return frozenset(RETRIEVAL_TOOL_NAMES[mode] for mode in RetrievalMode if self.permits(mode))


    def permits(self, mode: RetrievalMode) -> bool:
        return self.retrieval_mode is None or mode == self.retrieval_mode


    @staticmethod
    def parse_mode(value: RetrievalMode | str | None) -> RetrievalMode | None:
        if value is None or value == "auto":
            return None
        try:
            return value if isinstance(value, RetrievalMode) else RetrievalMode(value)
        except ValueError as exc:
            raise RavenError(
                ErrorCode.INVALID_RETRIEVAL_MODE,
                f"Unknown retrieval mode '{value}'.",
            ) from exc
