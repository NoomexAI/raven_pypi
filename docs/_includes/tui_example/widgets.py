"""Small chat-area widgets for knowledge navigation and creation."""

from __future__ import annotations

import re
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option



class KnowledgeList(Vertical):

    def __init__(self, knowledges: list[dict[str, Any]]) -> None:
        super().__init__(classes="chat-message knowledge-results")
        self.knowledges = knowledges


    def compose(self) -> ComposeResult:
        yield Static("Knowledges", classes="message-speaker")
        if not self.knowledges:
            yield Static("No knowledges yet. Use /knowledges/create to add one.")
            return
        yield Static("Click a knowledge, or focus the list and use ↑/↓ then Enter.", classes="list-hint")
        yield OptionList(*(
            Option(
                Text(
                    f"{item['name']}  ·  {item['file_count']} "
                    f"{'file' if item['file_count'] == 1 else 'files'}\n"
                    f"{item.get('user_summary') or 'No summary'}"
                ),
                id=f"/knowledges/{item['name']}",
            ) for item in self.knowledges
        ), classes="knowledge-list", markup=False)



class KnowledgeDetails(Vertical):

    def __init__(self, details: dict[str, Any]) -> None:
        super().__init__(classes="chat-message knowledge-details")
        self.metadata = details["metadata"]
        self.files = details["files"]


    def compose(self) -> ComposeResult:
        yield Static(self.metadata["name"], classes="message-speaker", markup=False)
        yield Static(self.metadata.get("user_summary") or "No summary", markup=False)
        yield Static(f"Created: {self.metadata.get('created_at', 'Unknown')}", classes="list-hint", markup=False)
        yield Static(f"Files ({len(self.files)})", classes="details-heading")
        if not self.files:
            yield Static(f"No files ingested yet. Use /knowledges/{self.metadata['name']}/ingest or Attach.", markup=False)
        for file in self.files:
            yield Static(
                f"{file['file_name']}\n"
                f"  {file.get('section_count', 0)} sections · {file.get('chunk_count', 0)} chunks",
                classes="knowledge-file", markup=False,
            )



class CreateKnowledgeForm(Vertical):
    """An inline form, so knowledge creation stays in the chat workspace."""

    class Submitted(Message):

        def __init__(self, form: CreateKnowledgeForm, name: str, summary: str) -> None:
            super().__init__()
            self.form = form
            self.name = name
            self.summary = summary


    def __init__(self) -> None:
        super().__init__(classes="chat-message knowledge-form")


    def compose(self) -> ComposeResult:
        yield Static("Create a knowledge", classes="message-speaker")
        yield Static("Name · letters, numbers, underscores or hyphens", classes="list-hint")
        yield Input(placeholder="engineering", classes="knowledge-name")
        yield Static("Summary · optional", classes="list-hint")
        yield Input(placeholder="Engineering reference documents", classes="knowledge-summary")
        yield Static("", classes="form-error", markup=False)
        with Horizontal(classes="form-actions"):
            yield Button("Create", classes="create-knowledge", compact=True)
            yield Button("Cancel", classes="cancel-knowledge", compact=True)


    def on_mount(self) -> None:
        self.query_one(".knowledge-name", Input).focus()


    @on(Input.Submitted, ".knowledge-name")
    def focus_summary(self, event: Input.Submitted) -> None:
        event.stop()
        self.query_one(".knowledge-summary", Input).focus()


    @on(Input.Submitted, ".knowledge-summary")
    @on(Button.Pressed, ".create-knowledge")
    def submit(self, event: Input.Submitted | Button.Pressed) -> None:
        event.stop()
        name = self.query_one(".knowledge-name", Input).value.strip()
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]+", name)
            or not name.strip("_")
            or name.strip("_").casefold() == "create"
        ):
            self.show_error("Use letters, numbers, _ or -. The name 'create' is reserved for the command.")
            return
        summary = self.query_one(".knowledge-summary", Input).value.strip()
        self.post_message(self.Submitted(self, name, summary))


    @on(Button.Pressed, ".cancel-knowledge")
    async def cancel(self, event: Button.Pressed) -> None:
        event.stop()
        await self.remove()
        self.app.query_one("#message").focus()


    def set_busy(self, busy: bool) -> None:
        for widget in self.query("Input, Button"):
            widget.disabled = busy


    def show_error(self, message: str) -> None:
        self.query_one(".form-error", Static).update(message)
