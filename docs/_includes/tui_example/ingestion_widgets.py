"""A local-file ingestion form and operation progress, kept inside the transcript."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Input, LoadingIndicator, Select, Static



class IngestionForm(Vertical):

    class Submitted(Message):

        def __init__(self, form: IngestionForm, knowledge_name: str, source_path: str) -> None:
            super().__init__()
            self.form = form
            self.knowledge_name = knowledge_name
            self.source_path = source_path


    def __init__(self, knowledge_names: list[str], knowledge_name: str | None = None) -> None:
        super().__init__(classes="chat-message ingestion-form")
        self.knowledge_names = knowledge_names
        self.knowledge_name = knowledge_name


    def compose(self) -> ComposeResult:
        yield Static("Ingest a document", classes="message-speaker")
        yield Static("Target knowledge", classes="field-label")
        yield Select(
            [(name, name) for name in self.knowledge_names],
            value=self.knowledge_name if self.knowledge_name is not None else Select.NULL,
            prompt="Choose a knowledge", id="ingestion-knowledge",
            disabled=self.knowledge_name is not None,
        )
        yield Static("Local file path", classes="field-label")
        yield Input(placeholder="E:\\documents\\reference.txt", id="ingestion-path")
        yield Static(
            "Accepts .txt, .md, .pdf, and .docx. Relative paths use the launch directory. "
            "The original file is not moved. Cloud LLMs receive document text for metadata extraction.",
            classes="list-hint",
        )
        yield Static("", classes="form-error", markup=False)
        with Horizontal(classes="form-actions"):
            yield Button("Ingest", classes="start-ingestion", compact=True)
            yield Button("Cancel", classes="cancel-ingestion-form", compact=True)


    def on_mount(self) -> None:
        self.query_one("#ingestion-path" if self.knowledge_name is not None else "#ingestion-knowledge").focus()


    @on(Select.Changed, "#ingestion-knowledge")
    def select_knowledge(self, event: Select.Changed) -> None:
        event.stop()
        if isinstance(event.value, str):
            self.query_one("#ingestion-path", Input).focus()


    @on(Input.Submitted, "#ingestion-path")
    @on(Button.Pressed, ".start-ingestion")
    def submit(self, event: Input.Submitted | Button.Pressed) -> None:
        event.stop()
        knowledge = self.query_one("#ingestion-knowledge", Select).value
        path = self.query_one("#ingestion-path", Input).value.strip()
        if not isinstance(knowledge, str):
            self.show_error("Choose a target knowledge first.")
        elif not path:
            self.show_error("Enter a local file path.")
        else:
            self.post_message(self.Submitted(self, knowledge, path))


    @on(Button.Pressed, ".cancel-ingestion-form")
    async def cancel(self, event: Button.Pressed) -> None:
        event.stop()
        await self.remove()
        self.app.query_one("#message").focus()


    def set_busy(self, busy: bool) -> None:
        for widget in self.query("Input, Button"):
            widget.disabled = busy
        self.query_one(Select).disabled = busy or self.knowledge_name is not None


    def show_error(self, message: str) -> None:
        self.query_one(".form-error", Static).update(message)



class IngestionProgress(Vertical):

    def __init__(self) -> None:
        super().__init__(classes="chat-message ingestion-progress")
        self.entries: list[tuple[str, str]] = []


    def compose(self) -> ComposeResult:
        yield Static("Document ingestion", classes="message-speaker")
        yield Static("Preparing…", classes="ingestion-stages", markup=False)
        yield LoadingIndicator(classes="ingestion-spinner")
        with Horizontal(classes="form-actions"):
            yield Button("Stop ingestion", classes="stop-ingestion", compact=True)


    def update_progress(self, stage: str, text: str) -> None:
        if self.entries and self.entries[-1][0] == stage:
            self.entries[-1] = (stage, text)
        else:
            self.entries.append((stage, text))
            self.entries = self.entries[-8:]
        self.query_one(".ingestion-stages", Static).update("\n".join(text for _, text in self.entries))


    def finish(self, text: str) -> None:
        self.update_progress("outcome", text)
        self.query_one(".ingestion-spinner").display = False
        self.query_one(".stop-ingestion", Button).disabled = True
