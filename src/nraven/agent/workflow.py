"""Pure translation from LlamaIndex workflow events to Raven events."""

from __future__ import annotations

import json
from typing import Any

from llama_index.core.agent.workflow import (
    AgentOutput,
    AgentStream,
    ToolCall,
    ToolCallResult,
)

from ..core.errors import error_payload
from ..core.events import Event, EventType
from .policy import RETRIEVAL_TOOL_NAMES


class WorkflowEventTranslator:
    """Translate one agent workflow while retaining call correlation state."""

    def __init__(self, max_iterations: int) -> None:
        if max_iterations <= 0:
            raise ValueError("max_iterations must be positive")

        self._max_iterations = max_iterations
        self._next_step = 0
        self._call_steps: dict[str, int] = {}
        self._agent_output_count = 0
        self.knowledge_tool_used = False
        self.tool_call_count = 0
        self.iteration_limit_reached = False


    def translate(self, workflow_event: Any) -> list[Event]:
        if isinstance(workflow_event, AgentStream):
            return self._translate_stream(workflow_event)
        if isinstance(workflow_event, AgentOutput):
            return self._translate_agent_output()
        if isinstance(workflow_event, ToolCall):
            return [self._translate_tool_call(workflow_event)]
        if isinstance(workflow_event, ToolCallResult):
            return [self._translate_tool_result(workflow_event)]
        return []


    def _translate_agent_output(self) -> list[Event]:
        self._agent_output_count += 1
        if (
            self._agent_output_count < self._max_iterations
            or self.iteration_limit_reached
        ):
            return []

        self.iteration_limit_reached = True
        return [
            Event(
                type=EventType.CHAT_MAX_ITERATIONS,
                data={
                    "limit": self._max_iterations,
                    "action": "generate_final_response",
                },
            )
        ]


    def _translate_stream(self, workflow_event: AgentStream) -> list[Event]:
        events: list[Event] = []
        agent_name = getattr(workflow_event, "current_agent_name", None)
        thinking_delta = getattr(workflow_event, "thinking_delta", None)
        if isinstance(thinking_delta, str) and thinking_delta:
            events.append(
                Event(
                    type=EventType.CHAT_THINKING_DELTA,
                    data={"delta": thinking_delta, "agent": agent_name},
                )
            )

        delta = getattr(workflow_event, "delta", None)
        if isinstance(delta, str) and delta:
            events.append(
                Event(
                    type=EventType.CHAT_RESPONSE_DELTA,
                    data={"delta": delta, "agent": agent_name},
                )
            )
        return events


    def _translate_tool_call(self, workflow_event: ToolCall) -> Event:
        call_id = str(getattr(workflow_event, "tool_id", None) or "")
        if not call_id:
            call_id = f"tool-call-{self._next_step + 1}"
        name = str(getattr(workflow_event, "tool_name", "unknown"))
        arguments = getattr(workflow_event, "tool_kwargs", {})
        if not isinstance(arguments, dict):
            arguments = {}

        self._next_step += 1
        self.tool_call_count += 1
        self._call_steps[call_id] = self._next_step
        if (
            name in RETRIEVAL_TOOL_NAMES.values()
            or name == "get_sections"
            or (name == "list_sections" and arguments.get("get_content") is True)
        ):
            self.knowledge_tool_used = True
        return Event(
            type=EventType.CHAT_TOOL_CALL,
            data={
                "call_id": call_id,
                "step": self._next_step,
                "name": name,
                "arguments": arguments,
            },
        )


    def _translate_tool_result(self, workflow_event: ToolCallResult) -> Event:
        call_id = str(getattr(workflow_event, "tool_id", None) or "")
        name = str(getattr(workflow_event, "tool_name", "unknown"))
        step = self._call_steps.get(call_id, self._next_step)
        tool_output = getattr(workflow_event, "tool_output", None)
        content = getattr(tool_output, "content", "")
        payload = self._decode_payload(content)
        fatal = bool(getattr(tool_output, "is_error", False))

        data: dict[str, Any] = {
            "call_id": call_id,
            "step": step,
            "name": name,
            "ok": False if fatal else bool(payload.get("ok", True)),
            "ui_summary": self._as_dict(payload.get("ui_summary")),
            "evidence": self._evidence(payload.get("evidence")),
        }
        error = payload.get("error")
        if isinstance(error, dict):
            data["error"] = error
        if fatal:
            exception = getattr(tool_output, "exception", None)
            data["error"] = error_payload(
                exception if isinstance(exception, BaseException) else RuntimeError("Tool execution failed.")
            )
            data["fatal"] = True
        return Event(type=EventType.CHAT_TOOL_RESULT, data=data)


    @staticmethod
    def _decode_payload(content: Any) -> dict[str, Any]:
        if not isinstance(content, str):
            return {}
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}


    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}


    @staticmethod
    def _evidence(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        references: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
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
                references.append(
                    {
                        "knowledge_name": knowledge_name,
                        "file_name": file_name,
                        "section_id": section_id,
                    }
                )
        return references
