from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from llama_index.core.agent import FunctionAgent
from llama_index.core.agent.workflow.workflow_events import AgentOutput, AgentStream, ToolCall, ToolCallResult
from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.core.tools import FunctionTool

from ..conversation_session import ConversationSession
from ..events import Event, EventBus, EventType
from .context import AgentRunContext
from .policy import AgentPolicy
from .tools import build_tools as build_agent_tools


SYSTEM_PROMPT = """You are Raven, a part of a RAG framework named RAVEN (Retrieval Augmented Adaptive Epistemic Navigation), a grounded assistant operating over the user's RAVEN knowledge base.

Conversation scope:
- type: {conversation_type}
- bound knowledge: {knowledge_name}

Use only tools actually provided to you. The tool set is the source of truth for what you can access.
Interpret the user's request yourself and decide whether to answer directly or use one or more tools.
You control the operation sequence; do not follow a fixed retrieval-then-answer workflow.

Local conversations are restricted to their bound knowledge. Global conversations may search across
knowledges. Navigation in a global conversation requires an explicit knowledge_name; never silently reuse
one from an earlier turn.

Retrieval tools answer content questions. Navigation tools inspect the structure or read a specific
section. Memory tools search this conversation and save explicit preferences. Do not treat conversation
memory as knowledge-base evidence unless the user asks about the conversation itself.

When evidence is available, answer faithfully and identify knowledge, file, and section IDs where useful.
When evidence is missing or insufficient, say so instead of using general model knowledge. Do not invent
tools, sources, files, sections, or facts.

Tool recovery:
- A tool result with `ok: false` is a recoverable tool-use problem, not final evidence.
- Read its `error` and `next_action`, correct the arguments or choose a more appropriate tool, then retry
  when that is useful. Do not repeat the same invalid call unchanged.
- If a tool reports that no relevant content was found, use another permitted strategy or explain that
  the available evidence is insufficient.

Retrieval constraint for this run: {retrieval_mode}
"""


class AgentHarness:
    """Runs one autonomous LlamaIndex agent with explicit RAVEN policy."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def _emit(self, run: AgentRunContext, etype: EventType, data: dict[str, Any]) -> None:
        self._bus.publish(Event(type=etype, data=data, op_id=run.operation_id))

    def _prompt(self, session: ConversationSession, policy: AgentPolicy) -> str:
        facts = "\n".join(f"<fact>{fact}</fact>" for fact in session.facts)
        preference_block = f"\n[PREFERENCES]\n{facts}\n[/PREFERENCES]" if facts else ""
        return SYSTEM_PROMPT.format(
            conversation_type=session.conversation.type,
            knowledge_name=session.conversation.knowledge_name or "None",
            retrieval_mode=policy.retrieval_mode,
        ) + preference_block

    def build_tools(
        self,
        session: ConversationSession,
        run: AgentRunContext | None = None,
        policy: AgentPolicy | None = None,
    ) -> list[FunctionTool]:
        """Build the tool registry for one run.

        ``run`` is optional for compatibility inspection; real execution always
        supplies it so results can be recorded without mutable session queues.
        """
        return build_agent_tools(
            session,
            run,
            policy or AgentPolicy(),
            self._bus,
        )

    def _agent(self, session: ConversationSession, tools: list[FunctionTool], policy: AgentPolicy) -> FunctionAgent:
        return FunctionAgent(
            name="Raven",
            description="Raven grounded knowledge assistant",
            system_prompt=self._prompt(session, policy),
            tools=tools,
            llm=session.llm,
            streaming=True,
            allow_parallel_tool_calls=policy.allow_parallel_tool_calls,
        )

    async def _generate_title(self, session: ConversationSession, user_text: str, run: AgentRunContext) -> None:
        try:
            response = await session.llm.achat(messages=[
                ChatMessage(role=MessageRole.SYSTEM, content="Generate a short 4-6 word title for a conversation. Output only the title."),
                ChatMessage(role=MessageRole.USER, content=user_text),
            ])
            title = (response.message.content or "").strip()
            if title:
                await session.set_title(title)
                self._emit(run, EventType.CONVERSATION_TITLE_GENERATED, {
                    "conversation_id": session.conversation.conversation_id,
                    "title": title,
                })
        except Exception:
            # Title generation is a best-effort metadata enhancement.
            return

    async def run(
        self,
        session: ConversationSession,
        user_text: str,
        *,
        retrieval_mode: str = "auto",
        operation_id: str,
        max_iterations: int = 12,
    ) -> AsyncIterator[Event]:
        policy = AgentPolicy(retrieval_mode=retrieval_mode, max_iterations=max_iterations)
        if retrieval_mode != "auto" and retrieval_mode not in policy.allowed_tools(session.conversation.type):
            raise ValueError(
                f"retrieval tool '{retrieval_mode}' is unavailable for "
                f"{session.conversation.type} conversations"
            )
        run = AgentRunContext(operation_id=operation_id, bus=self._bus)
        final_output: AgentOutput | None = None
        try:
            if not session.title_generated:
                await self._generate_title(session, user_text, run)
            tools = self.build_tools(session, run, policy)
            if not tools:
                raise ValueError(f"no tools are available for retrieval_mode '{retrieval_mode}'")
            agent = self._agent(session, tools, policy)
            handler = agent.run(
                user_msg=user_text,
                memory=session.memory,
                max_iterations=policy.max_iterations,
                early_stopping_method="force",
            )
            async for workflow_event in handler.stream_events():
                if isinstance(workflow_event, AgentStream):
                    if workflow_event.thinking_delta:
                        event = Event(type=EventType.CHAT_DELTA, data={"kind": "thinking_chunk", "delta": workflow_event.thinking_delta}, op_id=operation_id)
                        self._bus.publish(event)
                        yield event
                    if workflow_event.delta:
                        event = Event(type=EventType.CHAT_DELTA, data={"kind": "response_chunk", "delta": workflow_event.delta}, op_id=operation_id)
                        self._bus.publish(event)
                        yield event
                elif isinstance(workflow_event, ToolCall):
                    step = run.next_step()
                    event = Event(type=EventType.CHAT_TOOL_CALL, data={"call_id": workflow_event.tool_id, "step": step, "name": workflow_event.tool_name, "args": workflow_event.tool_kwargs}, op_id=operation_id)
                    self._bus.publish(event)
                    yield event
                elif isinstance(workflow_event, ToolCallResult):
                    result = run.latest_result(workflow_event.tool_name)
                    tool_failed = workflow_event.tool_output.is_error or (
                        isinstance(result, dict) and result.get("ok") is False
                    )
                    error = (
                        str(workflow_event.tool_output.raw_output)
                        if workflow_event.tool_output.is_error
                        else result.get("error")
                        if isinstance(result, dict) and result.get("ok") is False
                        else None
                    )
                    event = Event(type=EventType.CHAT_TOOL_RESULT, data={
                        "call_id": workflow_event.tool_id,
                        "step": run.step,
                        "name": workflow_event.tool_name,
                        "ok": not tool_failed,
                        "result": result,
                        "error": error,
                        "next_action": result.get("next_action") if isinstance(result, dict) else None,
                    }, op_id=operation_id)
                    self._bus.publish(event)
                    yield event
                elif isinstance(workflow_event, AgentOutput):
                    final_output = workflow_event
            stop = await handler
            if isinstance(stop, AgentOutput):
                final_output = stop
            elif hasattr(stop, "result") and isinstance(stop.result, AgentOutput):
                final_output = stop.result

            await session.persist(handler)
            reply = (final_output.response.content if final_output and final_output.response else "") or ""
            event = Event(type=EventType.CHAT_COMPLETE, data={"reply": reply}, op_id=operation_id)
            self._bus.publish(event)
            yield event
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            event = Event(type=EventType.ERROR, data={"op": "chat", "error": str(exc)}, op_id=operation_id)
            self._bus.publish(event)
            yield event
            raise
