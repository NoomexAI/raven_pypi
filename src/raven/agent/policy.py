from __future__ import annotations

from dataclasses import dataclass

from .catalog import (
    GLOBAL_TOOL_NAMES,
    LOCAL_TOOL_NAMES,
    NAVIGATION_TOOL_NAMES,
    MEMORY_TOOL_NAMES,
    RETRIEVAL_TOOLS,
)


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    """Immutable runtime constraints for one autonomous agent run."""

    retrieval_mode: str = "auto"
    max_iterations: int = 12
    allow_parallel_tool_calls: bool = False

    def __post_init__(self) -> None:
        if self.retrieval_mode != "auto" and self.retrieval_mode not in RETRIEVAL_TOOLS:
            raise ValueError(f"unknown retrieval_mode: {self.retrieval_mode}")
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be positive")

    def allowed_tools(self, conversation_type: str) -> frozenset[str]:
        retrieval = LOCAL_TOOL_NAMES if conversation_type == "local" else GLOBAL_TOOL_NAMES
        if self.retrieval_mode != "auto":
            if conversation_type == "local" and self.retrieval_mode not in LOCAL_TOOL_NAMES:
                retrieval = set()
            else:
                retrieval = {self.retrieval_mode}
        return frozenset(retrieval | MEMORY_TOOL_NAMES | NAVIGATION_TOOL_NAMES)
