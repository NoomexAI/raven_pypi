"""Public contracts for event-first agent runs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from llama_index.core.base.llms.types import MessageRole, ToolCallBlock
from llama_index.core.llms import ChatMessage
from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import ErrorCode, RavenError
from ..core.events import Event, EventType
from ..core.operations import OperationStatus, OperationTask


class EvidenceReference(BaseModel):
    """One verifiable knowledge section used during an agent run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    knowledge_name: str
    file_name: str
    section_id: str



class AgentRunResult(BaseModel):
    """A convenient projection built entirely from operation events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: UUID
    thinking: str = ""
    response: str
    iteration_limit_reached: bool = False
    evidence: list[EvidenceReference] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    reconstructed_sources: list[dict[str, Any]] = Field(default_factory=list)



@dataclass(slots=True)
class AgentTranscript:
    """Model-compatible conversation messages produced by one agent run."""

    _messages: list[ChatMessage]

    @classmethod
    def from_query(cls, query: str) -> "AgentTranscript":
        return cls(
            _messages=[ChatMessage.from_str(query, role=MessageRole.USER)]
        )


    def add_tool_call(
        self,
        *,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> None:
        self._messages.append(
            ChatMessage(
                role=MessageRole.ASSISTANT,
                blocks=[
                    ToolCallBlock(
                        tool_call_id=call_id,
                        tool_name=name,
                        tool_kwargs=arguments,
                    )
                ],
            )
        )


    def add_tool_result(
        self,
        *,
        call_id: str,
        name: str,
        content: str,
    ) -> None:
        self._messages.append(
            ChatMessage(
                role=MessageRole.TOOL,
                content=content,
                additional_kwargs={
                    "tool_call_id": call_id,
                    "tool_name": name,
                },
            )
        )


    def add_assistant_response(self, response: str) -> None:
        self._messages.append(
            ChatMessage.from_str(response, role=MessageRole.ASSISTANT)
        )


    def messages(self) -> list[ChatMessage]:
        """Return independent message copies for persistence or inspection."""
        return [message.model_copy(deep=True) for message in self._messages]



@dataclass(frozen=True, slots=True)
class RetrievalPipelines:
    """The four retrieval implementations available to agent tools."""

    embedded: Any
    hierarchical: Any
    agreement: Any
    vector_conditioned: Any



class AgentRun:
    """A replayable view over one running agent operation."""

    def __init__(self, task: OperationTask, transcript: AgentTranscript) -> None:
        self._task = task
        self._transcript = transcript


    @property
    def operation_id(self) -> UUID:
        return self._task.operation_id


    @property
    def status(self) -> OperationStatus:
        return self._task.status


    @property
    def task(self) -> OperationTask:
        return self._task


    def conversation_messages(self) -> list[ChatMessage]:
        """Return the model-compatible transcript for this completed run."""
        return self._transcript.messages()


    @property
    def stream(self) -> AsyncIterator[Event]:
        """Return a fresh iterator over this run's complete event stream."""
        return self.events()


    async def events(self, after_event_id: int = 0) -> AsyncIterator[Event]:
        """Yield replayed and live events after the supplied cursor."""
        if self._task.is_root:
            async for event in self._task.operation.events(
                after_event_id=after_event_id
            ):
                yield event
                if event.is_final:
                    return
            return

        terminal_task_events = {
            EventType.OPERATION_TASK_COMPLETED,
            EventType.OPERATION_TASK_FAILED,
            EventType.OPERATION_TASK_CANCELLED,
        }
        async for event in self._task.events(after_event_id=after_event_id):
            yield event
            if (
                event.task_id == self._task.task_id
                and event.type in terminal_task_events
            ):
                return


    async def collect(self) -> AgentRunResult:
        """Collect this run's events into a final structured result."""
        response_parts: list[str] = []
        thinking_parts: list[str] = []
        final_response: str | None = None
        iteration_limit_reached = False
        evidence: dict[tuple[str, str, str], EvidenceReference] = {}
        reconstructed_sources: list[dict[str, Any]] = []
        tool_calls: dict[str, dict[str, Any]] = {}
        tool_order: list[str] = []
        terminal_task_events = {
            EventType.OPERATION_TASK_COMPLETED,
            EventType.OPERATION_TASK_FAILED,
            EventType.OPERATION_TASK_CANCELLED,
        }

        await self._task.result()
        async for event in self.events():
            if event.type == EventType.CHAT_RESPONSE_DELTA:
                delta = event.data.get("delta")
                if isinstance(delta, str):
                    response_parts.append(delta)

            elif event.type == EventType.CHAT_THINKING_DELTA:
                delta = event.data.get("delta")
                if isinstance(delta, str):
                    thinking_parts.append(delta)

            elif event.type == EventType.CHAT_TOOL_CALL:
                call_id = event.data.get("call_id")
                if isinstance(call_id, str):
                    tool_calls[call_id] = dict(event.data)
                    tool_order.append(call_id)

            elif event.type == EventType.CHAT_TOOL_RESULT:
                self._collect_tool_result(
                    event,
                    tool_calls,
                    tool_order,
                    evidence,
                )

            elif event.type == EventType.CHAT_MAX_ITERATIONS:
                iteration_limit_reached = True

            elif event.type == EventType.RECONSTRUCTION_FILE:
                reconstructed_sources.append(dict(event.data))

            elif event.type == EventType.CHAT_COMPLETED:
                value = event.data.get("response")
                if isinstance(value, str):
                    final_response = value
                if event.data.get("iteration_limit_reached") is True:
                    iteration_limit_reached = True
                self._collect_evidence(event.data.get("evidence"), evidence)

            if (
                event.task_id == self._task.task_id
                and event.type in terminal_task_events
            ):
                break

        self._raise_for_terminal_status()
        return AgentRunResult(
            operation_id=self.operation_id,
            thinking="".join(thinking_parts),
            response=final_response if final_response is not None else "".join(response_parts),
            iteration_limit_reached=iteration_limit_reached,
            evidence=list(evidence.values()),
            tool_calls=[tool_calls[call_id] for call_id in tool_order],
            reconstructed_sources=reconstructed_sources,
        )


    async def cancel(self) -> None:
        """Cancel the run and wait until cancellation is terminal."""
        await self._task.cancel()
        try:
            await self._task.result()
        except RavenError as exc:
            if exc.code != ErrorCode.OPERATION_CANCELLED:
                raise


    async def wait(self) -> OperationStatus:
        """Wait for the operation and return its terminal status."""
        try:
            await self._task.result()
        except Exception:
            pass
        return self._task.status


    def _raise_for_terminal_status(self) -> None:
        if self._task.status == OperationStatus.CANCELLED:
            raise RavenError(
                ErrorCode.OPERATION_CANCELLED,
                f"Operation '{self.operation_id}' was cancelled.",
            )
        if self._task.status != OperationStatus.FAILED:
            return

        payload = self._task.operation.error or {}
        code_value = payload.get("code")
        try:
            code = ErrorCode(code_value)
        except (TypeError, ValueError):
            code = ErrorCode.INTERNAL_ERROR
        message = payload.get("message")
        raise RavenError(
            code,
            message if isinstance(message, str) else "The agent run failed.",
            details=payload.get("details") if isinstance(payload.get("details"), dict) else None,
        )


    @classmethod
    def _collect_tool_result(
        cls,
        event: Event,
        tool_calls: dict[str, dict[str, Any]],
        tool_order: list[str],
        evidence: dict[tuple[str, str, str], EvidenceReference],
    ) -> None:
        call_id = event.data.get("call_id")
        if not isinstance(call_id, str):
            return
        if call_id not in tool_calls:
            tool_calls[call_id] = {
                "call_id": call_id,
                "step": event.data.get("step"),
                "name": event.data.get("name"),
            }
            tool_order.append(call_id)
        tool_calls[call_id].update(
            {
                "ok": event.data.get("ok"),
                "ui_summary": event.data.get("ui_summary", {}),
                "error": event.data.get("error"),
            }
        )
        cls._collect_evidence(event.data.get("evidence"), evidence)


    @staticmethod
    def _collect_evidence(
        value: Any,
        evidence: dict[tuple[str, str, str], EvidenceReference],
    ) -> None:
        if not isinstance(value, list):
            return
        for item in value:
            if not isinstance(item, dict):
                continue
            try:
                reference = EvidenceReference.model_validate(item)
            except ValueError:
                continue
            key = (
                reference.knowledge_name,
                reference.file_name,
                reference.section_id,
            )
            evidence[key] = reference
