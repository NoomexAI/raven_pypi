"""User-facing orchestration for one Raven conversation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from ..agent.contracts import AgentRun, AgentRunResult
from ..agent.harness import AgentHarness
from ..agent.policy import RetrievalMode
from ..core.errors import ErrorCode, RavenError
from ..core.events import Event
from ..core.operations import OperationStatus
from ..data_management.conversation_manager import Conversation


class SessionRun:
    """Session-owned view of one harness run."""

    def __init__(
        self,
        agent_run: AgentRun,
        conversation: Conversation,
        user_query: str,
        release: Any,
    ) -> None:
        self._agent_run = agent_run
        self._conversation = conversation
        self._user_query = user_query
        self._release = release
        self._finalization_error: RavenError | None = None
        self._released = False
        self._finalization_task: asyncio.Task[None] | None = None


    @property
    def operation_id(self) -> UUID:
        return self._agent_run.operation_id


    @property
    def status(self) -> OperationStatus:
        return self._agent_run.status


    @property
    def stream(self) -> AsyncIterator[Event]:
        return self.events()


    async def events(self, after_event_id: int = 0) -> AsyncIterator[Event]:
        async for event in self._agent_run.events(after_event_id):
            yield event


    async def collect(self) -> AgentRunResult:
        try:
            result = await self._agent_run.collect()
        finally:
            await self._wait_for_finalization()

        self._raise_finalization_error()
        return result


    async def cancel(self) -> None:
        await self._agent_run.cancel()
        await self._wait_for_finalization()
        self._raise_finalization_error()


    async def wait(self) -> OperationStatus:
        status = await self._agent_run.wait()
        await self._wait_for_finalization()
        self._raise_finalization_error()
        return status


    async def _finalize(self) -> None:
        try:
            await self._agent_run.collect()
        except Exception:
            return

        try:
            await self._conversation.append_turn(
                self._agent_run.conversation_messages()
            )
        except Exception as exc:
            self._finalization_error = RavenError(
                ErrorCode.PERSISTENCE_FAILED,
                "The completed conversation turn could not be persisted.",
                details={"operation_id": str(self.operation_id)},
            )
            self._finalization_error.__cause__ = exc
        finally:
            self._release_once()


    async def _wait_for_finalization(self) -> None:
        if self._finalization_task is not None:
            await self._finalization_task
        self._release_once()


    def start_finalization(self) -> None:
        """Start tracked turn finalization after session registration."""
        if self._finalization_task is not None:
            raise RuntimeError("Session run finalization has already started.")
        self._finalization_task = asyncio.create_task(
            self._finalize(),
            name=f"raven-session-run-{self.operation_id}",
        )


    def _raise_finalization_error(self) -> None:
        if self._finalization_error is not None:
            raise self._finalization_error


    def _release_once(self) -> None:
        if self._released:
            return
        self._released = True
        self._release(self)



class Session:
    """Connect one conversation to one shared agent harness."""

    def __init__(
        self,
        conversation: Conversation,
        harness: AgentHarness,
        *,
        llm: Any,
        embed_model: Any,
        memory_token_limit: int = 4000,
        memory_top_k: int = 5,
    ) -> None:
        if llm is None:
            raise ValueError("llm is required")
        if embed_model is None:
            raise ValueError("embed_model is required")
        if memory_token_limit <= 0:
            raise ValueError("memory_token_limit must be positive")
        if memory_top_k <= 0:
            raise ValueError("memory_top_k must be positive")

        self.conversation = conversation
        self.harness = harness
        self._llm = llm
        self._embed_model = embed_model
        self._memory_token_limit = memory_token_limit
        self._memory_top_k = memory_top_k
        self._turn_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._active_runs: set[SessionRun] = set()
        self._started = False
        self._closed = False


    @property
    def conversation_id(self) -> str:
        return self.conversation.conversation_id


    @property
    def is_started(self) -> bool:
        return self._started and not self._closed


    async def start(self) -> None:
        """Open the conversation and initialize its memory resources."""
        async with self._lifecycle_lock:
            if self.is_started:
                return
            if self._closed:
                raise RavenError(
                    ErrorCode.CONVERSATION_CLOSED,
                    f"Session for conversation '{self.conversation_id}' is closed.",
                )

            await self.conversation.start()
            await self.conversation.initialize_memory(
                self._llm,
                self._embed_model,
                token_limit=self._memory_token_limit,
                memory_top_k=self._memory_top_k,
            )
            self._started = True


    async def close(self) -> None:
        """Cancel active turns without closing shared manager resources."""
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._started = False
            active_runs = list(self._active_runs)

        await asyncio.gather(
            *(run.cancel() for run in active_runs),
            return_exceptions=True,
        )


    async def generate_response(
        self,
        user_query: str,
        *,
        retrieval_mode: RetrievalMode | str | None = None,
    ) -> SessionRun:
        """Start one serialized autonomous turn for this conversation."""
        self._ensure_started()
        query = user_query.strip() if isinstance(user_query, str) else ""
        if not query:
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "user_query must be a non-empty string.",
            )

        await self._turn_lock.acquire()
        if self._closed:
            self._turn_lock.release()
            raise RavenError(
                ErrorCode.CONVERSATION_CLOSED,
                f"Session for conversation '{self.conversation_id}' is closed.",
            )

        try:
            agent_run = await self.harness.start(
                self.conversation,
                query,
                retrieval_mode=retrieval_mode,
            )
            session_run = SessionRun(
                agent_run,
                self.conversation,
                query,
                self._release_run,
            )
            self._active_runs.add(session_run)
            session_run.start_finalization()
            return session_run
        except Exception:
            self._turn_lock.release()
            raise


    def _release_run(self, run: SessionRun) -> None:
        self._active_runs.discard(run)
        self._turn_lock.release()


    def _ensure_started(self) -> None:
        if self._closed:
            raise RavenError(
                ErrorCode.CONVERSATION_CLOSED,
                f"Session for conversation '{self.conversation_id}' is closed.",
            )
        if not self._started:
            raise RavenError(
                ErrorCode.CONVERSATION_NOT_STARTED,
                f"Session for conversation '{self.conversation_id}' has not been started.",
            )
