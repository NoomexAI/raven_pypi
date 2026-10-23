"""Conversation navigation and canonical history, without running the agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, OptionList, Select, Static
from textual.widgets.option_list import Option

from source_widgets import SourceButton

if TYPE_CHECKING:
    from llama_index.core.llms import ChatMessage



def conversation_scope(metadata: dict[str, Any]) -> str:
    knowledge = metadata.get("knowledge_name")
    return f"Local · {knowledge}" if knowledge else "Global · All knowledges"



class ConversationList(Vertical):

    def __init__(self, conversations: list[dict[str, Any]]) -> None:
        super().__init__(classes="chat-message conversation-results")
        self.conversations = conversations


    def compose(self) -> ComposeResult:
        yield Static("Conversations", classes="message-speaker")
        if not self.conversations:
            yield Static("No conversations yet. Use /conversations/create to start one.")
            return
        yield Static("Click a title, or focus the list and use ↑/↓ then Enter.", classes="list-hint")
        yield OptionList(*(
            Option(
                Text(
                    f"{item['title']}\n{conversation_scope(item)} · "
                    f"{item['conversation_id'][:8]}… · {item['created_at']}"
                ),
                id=f"/conversations/{item['conversation_id']}",
            ) for item in self.conversations
        ), classes="conversation-list", markup=False)



class CreateConversationForm(Vertical):

    class Submitted(Message):

        def __init__(self, form: CreateConversationForm, knowledge_name: str | None) -> None:
            super().__init__()
            self.form = form
            self.knowledge_name = knowledge_name


    def __init__(self, knowledge_names: list[str]) -> None:
        super().__init__(classes="chat-message conversation-form")
        self.knowledge_names = knowledge_names


    def compose(self) -> ComposeResult:
        yield Static("Create a conversation", classes="message-speaker")
        yield Static("Knowledge scope · optional", classes="field-label")
        yield Select(
            [("Global · All knowledges", ""), *((name, name) for name in self.knowledge_names)],
            value="", allow_blank=False, id="conversation-knowledge",
        )
        yield Static(
            "Global conversations can search across your knowledge base. "
            "Choose a knowledge only to restrict the conversation to it. "
            "Its title starts as New Conversation; RAVEN titles it after the first chat turn.",
            classes="list-hint",
        )
        yield Static("", classes="form-error", markup=False)
        with Horizontal(classes="form-actions"):
            yield Button("Create", classes="create-conversation", compact=True)
            yield Button("Cancel", classes="cancel-conversation", compact=True)


    def on_mount(self) -> None:
        self.query_one(Select).focus()


    @on(Button.Pressed, ".create-conversation")
    def submit(self, event: Button.Pressed) -> None:
        event.stop()
        knowledge = self.query_one(Select).value
        self.post_message(self.Submitted(self, knowledge if isinstance(knowledge, str) and knowledge else None))


    @on(Button.Pressed, ".cancel-conversation")
    async def cancel(self, event: Button.Pressed) -> None:
        event.stop()
        await self.remove()
        self.app.query_one("#message").focus()


    def set_busy(self, busy: bool) -> None:
        for widget in self.query("Select, Button"):
            widget.disabled = busy


    def show_error(self, message: str) -> None:
        self.query_one(".form-error", Static).update(message)



class ConversationHistory(Vertical):

    def __init__(
        self, metadata: dict[str, Any], messages: list[ChatMessage],
        turns: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(classes="chat-message conversation-history")
        self.metadata = metadata
        self.messages = messages
        self.turns = turns if turns is not None else [{"messages": messages, "has_sources": False}]


    def compose(self) -> ComposeResult:
        yield Static(self.metadata["title"], classes="message-speaker", markup=False)
        yield Static(
            f"{conversation_scope(self.metadata)}\n"
            f"ID: {self.metadata['conversation_id']}\nCreated: {self.metadata['created_at']}",
            classes="list-hint", markup=False,
        )
        visible = False
        for turn in self.turns:
            for message in turn["messages"]:
                role = message.role.value
                if role in {"user", "assistant"} and message.content:
                    visible = True
                    yield Static("You" if role == "user" else "RAVEN", classes="message-speaker")
                    yield Static(message.content, classes="history-text", markup=False)
                # Tool exchanges remain visible as activity, not huge retrieved contexts.
                for block in message.blocks:
                    if block.block_type == "tool_call":
                        visible = True
                        yield Static(f"Tool call · {block.tool_name}", classes="list-hint", markup=False)
                if role == "tool":
                    visible = True
                    name = message.additional_kwargs.get("tool_name", "tool")
                    yield Static(f"Tool result · {name} · saved (payload omitted)", classes="list-hint", markup=False)
            if turn["has_sources"] and any(
                message.role.value == "assistant" and message.content for message in turn["messages"]
            ):
                yield SourceButton(self.metadata["conversation_id"], turn["turn_id"])
        if not visible:
            yield Static("No messages yet. This conversation is ready for its first turn.", classes="empty-history")
