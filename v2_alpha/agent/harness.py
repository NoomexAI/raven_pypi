"""Event-first autonomous execution for Raven conversations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.llms import ChatMessage

from ..core.errors import ErrorCode, RavenError, error_payload
from ..core.events import Event, EventType
from ..core.operations import Operation, OperationManager
from ..data_management.conversation_manager import Conversation
from ..data_management.knowledge_base import KnowledgeBase
from ..pipelines.reconstructor import Reconstructor
from .contracts import AgentRun, RetrievalPipelines
from .policy import AgentPolicy, RetrievalMode
from .prompts import PromptBuilder, PromptBundle
from .tools import ToolBuilder
from .workflow import WorkflowEventTranslator


INSUFFICIENT_EVIDENCE_RESPONSE = (
    "I couldn't obtain enough verified evidence from the knowledge base to "
    "answer that reliably."
)


@dataclass(frozen=True, slots=True)
class _CachedPrompt:
    key: tuple[Any, ...]
    bundle: PromptBundle



class AgentHarness:
    """Start autonomous agent runs whose authoritative output is events."""

    def __init__(
        self,
        llm: Any,
        knowledge_base: KnowledgeBase,
        retrieval_pipelines: RetrievalPipelines,
        operation_manager: OperationManager,
        *,
        reconstructor: Reconstructor | None = None,
        max_iterations: int = 10,
        top_k: int = 3,
        agent_factory: Callable[..., Any] = FunctionAgent,
    ) -> None:
        if llm is None:
            raise ValueError("llm is required")
        if max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        if top_k <= 0:
            raise ValueError("top_k must be positive")

        self._llm = llm
        self._knowledge_base = knowledge_base
        self._retrieval_pipelines = retrieval_pipelines
        self._operation_manager = operation_manager
        self._reconstructor = reconstructor or Reconstructor(knowledge_base)
        self._max_iterations = max_iterations
        self._top_k = top_k
        self._agent_factory = agent_factory
        self._prompt_cache: dict[str, list[_CachedPrompt]] = {}


    async def start(
        self,
        conversation: Conversation,
        user_query: str,
        *,
        retrieval_mode: RetrievalMode | str | None = None,
    ) -> AgentRun:
        """Start one agent operation and return its replayable run handle."""
        query = user_query.strip() if isinstance(user_query, str) else ""
        if not query:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "user_query must be a non-empty string.",
            )
        policy = AgentPolicy.create(
            conversation,
            retrieval_mode,
            max_iterations=self._max_iterations,
            top_k=self._top_k,
        )

        async def worker(operation: Operation) -> None:
            await self._execute(
                conversation,
                query,
                policy,
                operation,
            )

        operation = await self._operation_manager.submit(
            "chat.generate_response",
            worker,
        )
        return AgentRun(operation)


    def invalidate_system_prompt(self, conversation_id: str) -> None:
        """Invalidate prompts cached for one conversation."""
        self._prompt_cache.pop(conversation_id, None)


    async def _execute(
        self,
        conversation: Conversation,
        query: str,
        policy: AgentPolicy,
        operation: Operation,
    ) -> None:
        handler: Any = None
        translator: WorkflowEventTranslator | None = None
        try:
            operation.raise_if_cancelled()
            tools = ToolBuilder(
                self._knowledge_base,
                self._retrieval_pipelines,
            ).build(
                conversation,
                policy,
                operation,
                on_preferences_changed=self.invalidate_system_prompt,
            )
            prompt = self._update_system_prompt(conversation, policy)
            history = await conversation.get_context_messages(
                initial_token_count=prompt.token_count,
            )
            operation.raise_if_cancelled()

            agent = self._agent_factory(
                llm=self._llm,
                tools=tools,
                system_prompt=prompt.text,
                streaming=True,
                allow_parallel_tool_calls=False,
                early_stopping_method="generate",
            )
            handler = agent.run(
                user_msg=query,
                chat_history=history,
                max_iterations=policy.max_iterations,
                early_stopping_method="generate",
            )
            translator = WorkflowEventTranslator(policy.max_iterations)
            evidence: dict[tuple[str, str, str], dict[str, str]] = {}
            pending_response_events: list[Event] = []

            async for workflow_event in handler.stream_events(expose_internal=False):
                operation.raise_if_cancelled()
                for event in translator.translate(workflow_event):
                    if (
                        event.type == EventType.CHAT_RESPONSE_DELTA
                        and translator.knowledge_tool_used
                        and not evidence
                    ):
                        pending_response_events.append(event)
                        continue

                    self._remember_evidence(event, evidence)
                    await operation.publish(event)
                    if event.type == EventType.CHAT_TOOL_RESULT and event.data.get("fatal") is True:
                        raise RavenError(
                            ErrorCode.INTERNAL_ERROR,
                            "An unrecoverable tool failure stopped the agent run.",
                        )
                    if evidence and pending_response_events:
                        for pending_event in pending_response_events:
                            await operation.publish(pending_event)
                        pending_response_events.clear()

            output = await handler
            response = self._response_text(output)
            if translator.knowledge_tool_used and not evidence:
                response = INSUFFICIENT_EVIDENCE_RESPONSE
                await operation.publish(
                    Event(
                        type=EventType.CHAT_RESPONSE_DELTA,
                        data={"delta": response, "agent": "Raven"},
                    )
                )
            elif pending_response_events:
                for pending_event in pending_response_events:
                    await operation.publish(pending_event)

            reconstructed_sources = await self._reconstructor.reconstruct(
                list(evidence.values()),
                operation=operation,
            ) if evidence else []
            await operation.publish(
                Event(
                    type=EventType.CHAT_COMPLETED,
                    data={
                        "response": response,
                        "evidence": list(evidence.values()),
                        "tool_call_count": translator.tool_call_count,
                        "reconstructed_file_count": len(reconstructed_sources),
                        "iteration_limit_reached": translator.iteration_limit_reached,
                    },
                )
            )
        except asyncio.CancelledError:
            await self._cancel_handler(handler)
            raise
        except Exception as exc:
            await self._cancel_handler(handler)
            failure = self._iteration_failure(exc, translator, policy)
            await operation.publish(
                Event(
                    type=EventType.CHAT_FAILED,
                    data={"error": error_payload(failure)},
                )
            )
            raise failure


    def _update_system_prompt(
        self,
        conversation: Conversation,
        policy: AgentPolicy,
    ) -> PromptBundle:
        preferences = conversation.get_preferences()
        key = (
            policy.conversation_type,
            policy.knowledge_name,
            policy.retrieval_mode,
            tuple(
                (preference["preference_id"], preference["text"])
                for preference in preferences
            ),
        )
        cached_prompts = self._prompt_cache.setdefault(
            conversation.conversation_id,
            [],
        )
        for cached in cached_prompts:
            if cached.key == key:
                return cached.bundle

        bundle = PromptBuilder.build(policy, preferences)
        cached_prompts.append(_CachedPrompt(key=key, bundle=bundle))
        return bundle


    @staticmethod
    def _remember_evidence(
        event: Event,
        evidence: dict[tuple[str, str, str], dict[str, str]],
    ) -> None:
        if event.type != EventType.CHAT_TOOL_RESULT:
            return
        value = event.data.get("evidence")
        if not isinstance(value, list):
            return
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
                key = (knowledge_name, file_name, section_id)
                evidence[key] = {
                    "knowledge_name": knowledge_name,
                    "file_name": file_name,
                    "section_id": section_id,
                }


    @staticmethod
    def _response_text(output: Any) -> str:
        response = getattr(output, "response", output)
        if isinstance(response, ChatMessage):
            return response.content or ""
        if isinstance(response, str):
            return response
        content = getattr(response, "content", None)
        if isinstance(content, str):
            return content
        return str(response)


    @staticmethod
    async def _cancel_handler(handler: Any) -> None:
        if handler is None:
            return
        cancel_run = getattr(handler, "cancel_run", None)
        is_done = getattr(handler, "is_done", None)
        if callable(is_done) and is_done():
            return
        if callable(cancel_run):
            await cancel_run()


    @staticmethod
    def _iteration_failure(
        error: Exception,
        translator: WorkflowEventTranslator | None,
        policy: AgentPolicy,
    ) -> Exception:
        if translator is None or not translator.iteration_limit_reached:
            return error
        return RavenError(
            ErrorCode.AGENT_MAX_ITERATIONS,
            "The agent reached its iteration limit and failed to generate a final response.",
            details={"limit": policy.max_iterations},
        )
