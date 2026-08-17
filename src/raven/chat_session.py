"""Compatibility facade for the legacy ChatSession API.

Conversation state now lives in :class:`ConversationSession` and autonomous
model behavior lives in :class:`AgentHarness`. This facade preserves the
existing constructor and ``stream`` entry point for Python callers.
"""

from __future__ import annotations

import json
from uuid import uuid4

from .agent.catalog import (
    GLOBAL_TOOL_NAMES,
    LOCAL_TOOL_NAMES,
    MEMORY_TOOL_NAMES,
    NAVIGATION_TOOL_NAMES,
    NON_RETRIEVAL_TOOL_NAMES,
    RETRIEVAL_TOOLS,
    TOOL_DESCRIPTIONS,
)
from .agent.harness import AgentHarness
from .conversation_session import ConversationSession
from .events import EventBus, EventType


class ChatSession(ConversationSession):
    """Legacy facade delegating execution to :class:`AgentHarness`."""

    @property
    def _tools(self):
        return self._build_tools()

    def _build_tools(self):
        """Compatibility inspection hook retained for existing callers."""
        return AgentHarness(getattr(self, "_bus", EventBus())).build_tools(self)

    async def _get_section_metadata(self, section_id: str, knowledge_name: str = "") -> str:
        return json.dumps(
            await self.get_section_metadata(section_id, knowledge_name),
            ensure_ascii=False,
        )

    async def stream(
        self,
        user_text: str,
        retrieval_mode: str = "auto",
        op_id: str | None = None,
    ) -> str:
        operation_id = op_id or uuid4().hex
        reply = ""
        async for event in AgentHarness(self._bus).run(
            self,
            user_text,
            retrieval_mode=retrieval_mode,
            operation_id=operation_id,
        ):
            if event.type is EventType.CHAT_COMPLETE:
                reply = str(event.data.get("reply", ""))
        return reply


__all__ = [
    "ChatSession",
    "GLOBAL_TOOL_NAMES",
    "LOCAL_TOOL_NAMES",
    "MEMORY_TOOL_NAMES",
    "NAVIGATION_TOOL_NAMES",
    "NON_RETRIEVAL_TOOL_NAMES",
    "RETRIEVAL_TOOLS",
    "TOOL_DESCRIPTIONS",
]
