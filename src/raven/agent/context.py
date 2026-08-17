from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..events import EventBus


@dataclass(slots=True)
class AgentRunContext:
    operation_id: str
    bus: EventBus
    evidence: set[str] = field(default_factory=set)
    tool_results: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    step: int = 0

    def record_tool_result(
        self,
        tool_name: str,
        result: dict[str, Any] | None,
        *,
        evidence: str | None = None,
    ) -> None:
        self.tool_results.setdefault(tool_name, []).append(result or {})
        if evidence:
            self.evidence.add(evidence)

    def next_step(self) -> int:
        self.step += 1
        return self.step

    def latest_result(self, tool_name: str) -> dict[str, Any] | None:
        values = self.tool_results.get(tool_name)
        return values.pop(0) if values else None
