"""One streamed turn: optional thinking, compact tool activity, and answer text."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Collapsible, LoadingIndicator, Static

if TYPE_CHECKING:
    from nraven.core.events import Event



class StreamingResponse(Vertical):

    def __init__(self) -> None:
        super().__init__(classes="chat-message streaming-response")
        self.response = ""
        self.thinking = ""
        self.tool_activity: dict[str, str] = {}
        self.tool_names: list[str] = []
        self.event_types: list[str] = []
        self.turn_id: str | None = None
        self.has_sources = False


    def compose(self) -> ComposeResult:
        yield Static("RAVEN", classes="message-speaker")
        with Collapsible(title="Thinking", collapsed=True, classes="chat-thinking"):
            with VerticalScroll(classes="thinking-scroll"):
                yield Static("", classes="thinking-text", markup=False)
        yield Static("", classes="tool-activity", markup=False)
        yield Static("", classes="response-text", markup=False)
        yield LoadingIndicator(classes="response-spinner")
        yield Static("Preparing the conversation…", classes="response-outcome list-hint", markup=False)


    def on_mount(self) -> None:
        self.query_one(".chat-thinking").display = False
        self.query_one(".tool-activity").display = False


    def apply_event(self, event: Event) -> str | None:
        """Project only UI-relevant events; never display raw retrieval contexts."""
        kind, data = event.type.value, event.data
        if kind not in self.event_types:
            self.event_types.append(kind)
        status = None
        if kind == "chat.thinking_delta":
            self.thinking += str(data.get("delta") or "")
            self.query_one(".thinking-text", Static).update(self.thinking)
            self.query_one(".chat-thinking").display = bool(self.thinking)
            status = "Thinking…"
        elif kind == "chat.response_delta":
            self.response += str(data.get("delta") or "")
            self.query_one(".response-text", Static).update(self.response)
            status = "Responding…"
        elif kind in {"chat.tool_call", "chat.tool_result"}:
            call_id = str(data.get("call_id") or data.get("name") or "tool")
            name = str(data.get("name") or "tool")
            if kind == "chat.tool_call":
                self.tool_names.append(name)
                text = f"{data.get('step', '?')}. {name} · running"
            else:
                state = "done" if data.get("ok") is True else "failed (agent may recover)"
                text = f"{data.get('step', '?')}. {name} · {state}"
                if data.get("ok") is True and data.get("evidence"):
                    self.has_sources = True
            self.tool_activity[call_id] = text
            self.query_one(".tool-activity", Static).update("\n".join(self.tool_activity.values()))
            self.query_one(".tool-activity").display = True
            status = f"Using {name}…" if kind == "chat.tool_call" else "Tool returned · Agent continuing…"
        elif kind == "conversation.memory_compaction.started":
            status = "Compacting conversation context…"
        elif kind == "conversation.memory_compaction.completed":
            status = "Context compacted · Agent continuing…"
        elif kind == "chat.max_iterations":
            status = "Tool-step limit reached · Waiting for the final answer…"
        elif kind.startswith("reconstruction."):
            status = "Reconstructing sources…"
        elif kind == "chat.completed":
            status = "Answer generated · Saving turn…"
        elif kind in {"conversation.turn_commit.completed", "conversation.turn_commit.reused"}:
            self.turn_id = str(data["turn_id"])
            status = "Turn committed · Finishing memory and title updates…"
        if status is not None:
            self.query_one(".response-outcome", Static).update(status)
        return status


    def finish(self, text: str, *, response: str | None = None) -> None:
        if response is not None:
            self.response = response
            self.query_one(".response-text", Static).update(response)
        self.query_one(".response-spinner").display = False
        self.query_one(".response-outcome", Static).update(text)
