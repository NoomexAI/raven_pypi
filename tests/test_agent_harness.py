from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from llama_index.core.agent.workflow.workflow_events import AgentOutput, AgentStream, ToolCall, ToolCallResult
from llama_index.core.llms import ChatMessage
from llama_index.core.tools import FunctionTool, ToolOutput

from raven.agent import AgentHarness, AgentPolicy
from raven.agent.catalog import GLOBAL_EMBEDDED_RETRIEVAL, LOCAL_EMBEDDED_RETRIEVAL
from raven.agent.context import AgentRunContext
from raven.agent.tools import _recoverable_error
from raven.events import EventBus, EventType


def fake_session(conversation_type: str = "global"):
    return SimpleNamespace(
        conversation=SimpleNamespace(type=conversation_type, knowledge_name=None),
        facts=[],
        title_generated=True,
        llm=object(),
        memory=object(),
    )


def test_policy_filters_retrieval_without_forcing_a_call():
    policy = AgentPolicy(retrieval_mode=GLOBAL_EMBEDDED_RETRIEVAL)
    allowed = policy.allowed_tools("global")

    assert GLOBAL_EMBEDDED_RETRIEVAL in allowed
    assert LOCAL_EMBEDDED_RETRIEVAL not in allowed
    assert "get_memory" in allowed
    assert "list_sections" in allowed
    assert policy.allow_parallel_tool_calls is False


def test_recoverable_tool_error_gives_the_agent_a_next_action():
    run = AgentRunContext(operation_id="op-1", bus=EventBus())

    result = json.loads(
        _recoverable_error(
            run,
            "list_files",
            ValueError("knowledge_name is required"),
            "Provide knowledge_name and retry.",
        )
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_tool_request"
    assert result["next_action"] == "Provide knowledge_name and retry."
    assert run.latest_result("list_files") == result


class FakeHandler:
    def __init__(self, output: AgentOutput):
        self.ctx = SimpleNamespace(to_dict=lambda: {})
        self._output = output

    async def stream_events(self):
        yield AgentStream(
            delta="verified answer",
            response="verified answer",
            current_agent_name="Raven",
            thinking_delta=None,
        )
        yield ToolCall(tool_name="global_embedded_retrieval", tool_kwargs={"user_query": "q"}, tool_id="call-42")
        yield ToolCallResult(
            tool_name="global_embedded_retrieval",
            tool_kwargs={"user_query": "q"},
            tool_id="call-42",
            tool_output=ToolOutput(
                content="full model context",
                tool_name="global_embedded_retrieval",
                raw_input={},
            ),
            return_direct=False,
        )
        yield self._output

    def __await__(self):
        async def finish():
            return self._output
        return finish().__await__()


class FakeAgent:
    def __init__(self, output: AgentOutput):
        self.output = output

    def run(self, **_kwargs):
        return FakeHandler(self.output)


class FakeHarness(AgentHarness):
    def _agent(self, session, tools, policy):
        output = AgentOutput(
            response=ChatMessage(role="assistant", content="verified answer"),
            current_agent_name="Raven",
        )
        return FakeAgent(output)

    def build_tools(self, session, run=None, policy=None):
        if run is not None:
            run.record_tool_result(
                GLOBAL_EMBEDDED_RETRIEVAL,
                {"kind": "retrieval", "section_ids": ["section-1"]},
                evidence="knowledge",
            )

        async def noop() -> str:
            return "ok"

        return [FunctionTool.from_defaults(async_fn=noop, name="noop")]


@pytest.mark.asyncio
async def test_harness_emits_correlated_tool_events_and_verified_reply():
    bus = EventBus()
    harness = FakeHarness(bus)
    session = fake_session()

    async def persist(_handler):
        return None

    session.persist = persist

    events = [
        event
        async for event in harness.run(
            session,
            "What is the verified answer?",
            retrieval_mode="auto",
            operation_id="op-1",
        )
    ]

    call = next(event for event in events if event.type is EventType.CHAT_TOOL_CALL)
    result = next(event for event in events if event.type is EventType.CHAT_TOOL_RESULT)
    complete = next(event for event in events if event.type is EventType.CHAT_COMPLETE)
    assert call.data["call_id"] == "call-42"
    assert result.data["call_id"] == "call-42"
    assert result.data["step"] == call.data["step"]
    assert complete.data["reply"] == "verified answer"
