"""RAVEN's chat-first demo: documents, models, and conversation navigation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.worker import Worker
from textual.widgets import Button, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option

from commands import help_text, matching_commands, resolve_command
from chat_widgets import StreamingResponse
from conversation_widgets import ConversationHistory, ConversationList, CreateConversationForm, conversation_scope
from ingestion_widgets import IngestionForm, IngestionProgress
from model_widgets import ConfiguredModels, ModelConfigurationForm, ModelProgress, OllamaModels
from raven_client import RavenClient
from source_widgets import SourceButton, SourceViewer
from widgets import CreateKnowledgeForm, KnowledgeDetails, KnowledgeList

if TYPE_CHECKING:
    from nraven.core.events import Event
    from nraven.data_management.conversation_manager import Conversation

RAVEN_HOME = Path(__file__).resolve().parent / "raven_home"



class PromptEditor(TextArea):
    """Multiline editor with explicit send and newline shortcuts."""

    BINDINGS = [
        Binding("enter", "submit", "Send", show=False, priority=True),
        Binding("shift+enter", "insert_newline", "New line", show=False),
    ]

    class Submitted(Message):
        """Request submission without coupling the editor to the app."""


    def action_submit(self) -> None:
        self.post_message(self.Submitted())


    def action_insert_newline(self) -> None:
        self.insert("\n")


    def on_resize(self) -> None:
        # Leave room for the transcript in shorter terminals.
        self.styles.min_height = 2 if self.app.size.height < 26 else 4
        self.styles.max_height = max(2, min(8, self.app.size.height // 5))



class ChatMessage(Vertical):
    """Keep a message's speaker and plain text together in the transcript."""

    def __init__(self, speaker: str, text: str) -> None:
        super().__init__(classes="chat-message")
        self.speaker = speaker
        self.text = text


    def compose(self) -> ComposeResult:
        yield Static(self.speaker, classes="message-speaker", markup=False)
        yield Static(self.text, classes="message-body", markup=False)



class RavenDemo(App[None]):
    """A chat shell with one routing path for typed and selected commands."""

    TITLE = "RAVEN · Document assistant"
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("ctrl+l", "focus_message", "Focus message"),
    ]

    def __init__(self, *, connect_backend: bool = True) -> None:
        super().__init__()
        self.raven_home = RAVEN_HOME
        self._menu_dismissed_for: str | None = None
        self.client = RavenClient(self.raven_home) if connect_backend else None
        self.backend_ready = False
        self._startup_complete = asyncio.Event()
        self._backend_busy = False
        self._knowledge_names: list[str] = []
        self.active_conversation: Conversation | None = None
        # Derived only from an explicitly opened conversation, never from browsing.
        self.conversation_knowledge: str | None = None
        self._ingestion_worker: Worker[None] | None = None
        self._chat_worker: Worker[None] | None = None
        self._chat_stopping = False


    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield Static("RAVEN", id="brand")
            yield Static("No conversation · /conversations to open one", id="context", markup=False)

        with Vertical(id="workspace"):
            with VerticalScroll(id="chat", can_focus=True):
                with Vertical(id="welcome"):
                    yield Static("Your documents. A conversation.", id="welcome-title")
                    yield Static(
                        "Create a knowledge, add a document, then ask questions "
                        "and inspect the sources.",
                        id="welcome-description",
                    )
                    yield Static(
                        "Start with /knowledges/create or /knowledges. "
                        "Use /models/configure, then Attach to ingest a document. "
                        "Open a conversation with /conversations, then ask a question.",
                        id="preview-note",
                    )

            with Vertical(id="composer-area"):
                yield Static("Ready · / for commands", id="status", markup=False)
                with Vertical(id="composer"):
                    yield PromptEditor(
                        placeholder="Message or /command…",
                        id="message",
                        highlight_cursor_line=False,
                        tab_behavior="focus",
                    )
                    with Horizontal(id="composer-actions"):
                        yield Button(
                            "Attach", id="ingest", compact=True,
                            tooltip="Ingest a local document into a knowledge",
                        )
                        yield Static(id="action-spacer")
                        yield Button(
                            "Send", id="send", compact=True,
                            tooltip="Send message", disabled=True,
                        )
                yield Static(
                    "Enter to send · Shift+Enter for a new line · Ctrl+C to quit",
                    id="shortcuts",
                )
        yield OptionList(id="command-menu", markup=False)


    async def on_mount(self) -> None:
        if self.client is None:
            await asyncio.to_thread(self.raven_home.mkdir, exist_ok=True)
        self.theme = "textual-dark"
        menu = self.query_one("#command-menu", OptionList)
        menu.border_title = "Commands"
        menu.border_subtitle = "↑/↓ choose · Enter run · Tab complete · Esc close"
        self.screen.screen_layout_refresh_signal.subscribe(
            self, lambda _: self.position_command_menu()
        )
        self.on_resize()
        self.query_one("#message", PromptEditor).focus()
        if self.client is not None:
            self.query_one("#status", Static).update("Starting RAVEN · No models are loaded")
            self.run_worker(self.start_backend(), name="RAVEN startup", group="backend")


    async def start_backend(self) -> None:
        assert self.client is not None
        try:
            await asyncio.to_thread(self.raven_home.mkdir, exist_ok=True)
            await self.client.start()
            self._knowledge_names = [item["name"] for item in await self.client.list_knowledges()]
            self.backend_ready = True
            self.query_one("#status", Static).update("Ready · /knowledges to browse · No models loaded")
        except Exception as error:
            self.query_one("#welcome").display = False
            chat = self.query_one("#chat", VerticalScroll)
            await chat.mount(ChatMessage("Backend error", str(error)))
            chat.scroll_end(animate=False)
            self.query_one("#status", Static).update("RAVEN could not start · Close and reopen after fixing the error")
        finally:
            self._startup_complete.set()


    async def on_unmount(self) -> None:
        if self.client is not None:
            await self.client.close()


    def on_resize(self) -> None:
        self.screen.set_class(self.size.height < 26, "compact-height")
        self.screen.set_class(self.size.width < 85, "narrow")
        for editor in self.query(PromptEditor):
            editor.on_resize()
        self.call_after_refresh(self.position_command_menu)


    @on(TextArea.Changed, "#message")
    def update_composer(self, event: TextArea.Changed) -> None:
        self.query_one("#send", Button).disabled = (
            self._chat_stopping if self._chat_worker is not None else not event.text_area.text.strip()
        )
        text = event.text_area.text
        if text != self._menu_dismissed_for:
            self._menu_dismissed_for = None
        menu = self.query_one("#command-menu", OptionList)
        if (
            not text.startswith("/")
            or any(character.isspace() for character in text)
            or text == self._menu_dismissed_for
        ):
            menu.display = False
            return

        choices = matching_commands(text, self._knowledge_names)
        menu.clear_options()
        menu.add_options(
            Option(Text(f"{command.path:<24} {command.description}", no_wrap=True), id=command.path)
            for command in choices
        )
        if not choices:
            menu.add_option(Option("No matching commands. Enter to check the typed path.", disabled=True))
        menu.highlighted = 0 if choices else None
        menu.display = True
        self.call_after_refresh(self.position_command_menu)


    def position_command_menu(self) -> None:
        menu = self.query_one("#command-menu", OptionList)
        if not menu.display:
            return
        composer = self.query_one("#composer").region
        available = composer.y - self.query_one("#top-bar").region.bottom
        if available < 3:
            menu.display = False
            return
        height = min(menu.option_count + 2, 8, available)
        menu.styles.width = composer.width
        menu.styles.height = height
        menu.styles.offset = (composer.x, composer.y - height)


    def dismiss_command_menu(self) -> None:
        self._menu_dismissed_for = self.query_one("#message", PromptEditor).text
        self.query_one("#command-menu", OptionList).display = False


    def on_key(self, event: events.Key) -> None:
        menu = self.query_one("#command-menu", OptionList)
        if not menu.display or event.key not in {"up", "down", "tab", "escape"}:
            return
        event.stop()
        event.prevent_default()
        if event.key == "up":
            menu.action_cursor_up()
        elif event.key == "down":
            menu.action_cursor_down()
        elif event.key == "escape":
            self.dismiss_command_menu()
            self.query_one("#message", PromptEditor).focus()
        elif menu.highlighted_option is not None and menu.highlighted_option.id:
            path = menu.highlighted_option.id
            self._menu_dismissed_for = path
            textbox = self.query_one("#message", PromptEditor)
            textbox.load_text(path)
            textbox.move_cursor(textbox.document.end)
            textbox.focus()
            menu.display = False


    def on_click(self, event: events.Click) -> None:
        menu = self.query_one("#command-menu", OptionList)
        if menu.display and not any(
            self.query_one(selector).region.contains(event.screen_x, event.screen_y)
            for selector in ("#command-menu", "#message")
        ):
            self.dismiss_command_menu()


    @on(PromptEditor.Submitted)
    async def show_message(self) -> None:
        menu = self.query_one("#command-menu", OptionList)
        if menu.display and menu.highlighted is not None:
            menu.action_select()
            return
        await self.submit_text(self.query_one("#message", PromptEditor).text)


    @on(Button.Pressed, "#send")
    async def send_or_stop(self) -> None:
        if self._chat_worker is not None:
            if not self._chat_stopping:
                self._chat_stopping = True
                self.query_one("#send", Button).disabled = True
                self.query_one("#status", Static).update("Stopping response… · Waiting for safe cleanup")
                self._chat_worker.cancel()
        else:
            await self.show_message()


    @on(OptionList.OptionSelected, "#command-menu")
    async def select_command(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_id is not None:
            await self.submit_text(event.option_id)


    @on(OptionList.OptionSelected, ".knowledge-list")
    async def open_knowledge(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_id is not None:
            await self.submit_text(event.option_id)


    @on(OptionList.OptionSelected, ".conversation-list")
    async def open_conversation(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_id is not None:
            await self.submit_text(event.option_id)


    async def submit_text(self, text: str) -> None:
        """Mouse and keyboard command choices enter here, just like typed paths."""
        textbox = self.query_one("#message", PromptEditor)
        text = text.strip()
        if not text:
            return

        if not text.startswith("/") and self.client is not None:
            await self.request_response(text)
            return

        self.query_one("#welcome").display = False
        chat = self.query_one("#chat", VerticalScroll)
        self.query_one("#command-menu", OptionList).display = False
        self._menu_dismissed_for = None
        textbox.load_text("")
        textbox.focus()
        await chat.mount(ChatMessage("You", text))
        status = "Displayed locally · Chat is not connected yet"
        if text.startswith("/"):
            command = resolve_command(text)
            if command is None:
                await chat.mount(ChatMessage("Command error", f"Unknown command: {text}\nUse /help to see available paths."))
                status = "Unknown command · /help for usage"
            elif command.path == "/help":
                await chat.mount(ChatMessage("Commands", help_text()))
                status = "Ready · / for commands"
            elif command.path == "/sources" and self.client is not None:
                buttons = list(self.query(SourceButton))
                if buttons:
                    await self.show_sources(buttons[-1])
                else:
                    await chat.mount(ChatMessage("Sources", "No saved source-backed response in this view yet. Casual answers and empty/failed retrievals have no sources."))
                    self.query_one("#status", Static).update("No sources to inspect")
                chat.scroll_end(animate=False)
                return
            elif command.path == "/knowledges/<name>/ingest" and self.client is not None:
                await self.request_ingestion_form(text.split("/")[2])
                return
            elif command.stage == 4 and self.client is not None:
                if self._backend_busy:
                    await chat.mount(ChatMessage("Please wait", "A backend action is still running."))
                    status = "Working · Please wait"
                else:
                    self._backend_busy = True
                    self.run_worker(self.show_conversation_route(text), name=text, group="backend")
                    status = "Loading conversations…"
            elif command.path in {"/models", "/models/configure", "/providers/ollama/models"} and self.client is not None:
                if self._backend_busy:
                    await chat.mount(ChatMessage("Please wait", "A backend action is still running."))
                    status = "Working · Please wait"
                else:
                    self._backend_busy = True
                    self.run_worker(self.show_model_route(text), name=text, group="backend")
                    status = "Preparing model view…"
            elif command.stage == 2 and self.client is not None:
                if self._backend_busy:
                    await chat.mount(ChatMessage("Please wait", "A backend action is still running."))
                    status = "Working · Please wait"
                elif command.path == "/knowledges":
                    self._backend_busy = True
                    self.run_worker(self.show_knowledge_route(text), name=text, group="backend")
                    status = "Loading knowledges…"
                elif not self.backend_ready:
                    await chat.mount(ChatMessage("Not ready", "RAVEN is not ready yet. Check the startup status below."))
                    status = "RAVEN not ready"
                elif command.path == "/knowledges/create":
                    for form in self.query(CreateKnowledgeForm):
                        await form.remove()
                    await chat.mount(CreateKnowledgeForm())
                    status = "Create a knowledge · Enter a name and optional summary"
                else:
                    self._backend_busy = True
                    self.run_worker(self.show_knowledge_route(text), name=text, group="backend")
                    status = "Reading knowledges…"
            else:
                await chat.mount(ChatMessage(
                    "Not connected yet",
                    f"{text}: {command.description}. This backend action is planned "
                    f"for stage {command.stage}; no data or models were changed.",
                ))
                status = "Command recognized · Backend action not connected"
        self.query_one("#status", Static).update(status)
        chat.scroll_end(animate=False)


    async def request_response(self, query: str) -> None:
        assert self.client is not None
        reason = None
        if not self.backend_ready:
            reason = "RAVEN is not ready yet. Check the startup status."
        elif self._backend_busy:
            reason = "A backend action is still running. Wait or stop the current response first."
        elif self.active_conversation is None:
            reason = "Open a conversation with /conversations or create one with /conversations/create."
        elif not self.client.models_ready:
            reason = "Configure both models with /models/configure before chatting."
        chat = self.query_one("#chat", VerticalScroll)
        self.query_one("#welcome").display = False
        if reason is not None:
            await chat.mount(ChatMessage("Cannot send", reason))
            self.query_one("#status", Static).update("Message not sent · Your draft is unchanged")
            chat.scroll_end(animate=False)
            return

        self._backend_busy = True
        self.dismiss_command_menu()
        self.query_one("#message", PromptEditor).load_text("")
        for notice in self.query(".empty-history"):
            notice.display = False
        await chat.mount(ChatMessage("You", query))
        response = StreamingResponse()
        await chat.mount(response)
        self._chat_worker = self.run_worker(self.generate_response(query, response), name="Chat turn", group="backend")
        self._chat_stopping = False
        button = self.query_one("#send", Button)
        button.label = "Stop"
        button.tooltip = "Stop the active response"
        button.disabled = False
        self.query_one("#ingest", Button).disabled = True
        self.query_one("#status", Static).update("Preparing conversation… · Retrieval: auto")
        chat.scroll_end(animate=False)


    async def generate_response(self, query: str, response: StreamingResponse) -> None:
        assert self.client is not None and self.active_conversation is not None
        conversation = self.active_conversation
        chat = self.query_one("#chat", VerticalScroll)
        try:
            async def show_event(event: Event) -> None:
                follow = chat.is_vertical_scroll_end
                status = response.apply_event(event)
                if status is not None:
                    self.query_one("#status", Static).update(status)
                if follow:
                    chat.scroll_end(animate=False)

            result = await self.client.generate_response(conversation, query, show_event)
            response.finish("Saved to conversation", response=result.response)
            cached_files = getattr(result, "reconstructed_sources", None)
            if response.turn_id and (response.has_sources or getattr(result, "evidence", None) or cached_files):
                await response.mount(SourceButton(conversation.conversation_id, response.turn_id, cached_files))
            self.query_one("#status", Static).update("Ready · Turn saved · Ask a follow-up")
        except asyncio.CancelledError:
            if self.is_running and response.is_attached:
                response.finish("Stopped · Displayed text may be incomplete. Reopen the conversation to inspect saved turns.")
                self.query_one("#status", Static).update("Response stopped · You can send another message")
            raise
        except Exception as error:
            if self.is_running and response.is_attached:
                response.finish(f"Failed: {error}\nDisplayed text may be incomplete; reopen the conversation to inspect saved turns.")
                self.query_one("#status", Static).update("Chat failed · You can try again")
        finally:
            self._backend_busy = False
            self._chat_worker = None
            self._chat_stopping = False
            if self.is_running:
                self.update_conversation_header(conversation.to_dict())
                button = self.query_one("#send", Button)
                button.label = "Send"
                button.tooltip = "Send message"
                button.disabled = not self.query_one("#message", PromptEditor).text.strip()
                self.query_one("#ingest", Button).disabled = False


    def update_conversation_header(self, metadata: dict[str, Any]) -> None:
        header = self.query_one("#context", Static)
        header.update(Text(
            f"{metadata['title']} · {conversation_scope(metadata)}",
            no_wrap=True, overflow="ellipsis",
        ))
        header.tooltip = f"{metadata['title']} · ID: {metadata['conversation_id']}"
        for history in self.query(ConversationHistory):
            history.metadata = metadata
            history.query_one(".message-speaker", Static).update(metadata["title"])


    async def show_conversation_route(self, path: str) -> None:
        assert self.client is not None
        chat = self.query_one("#chat", VerticalScroll)
        form = None
        try:
            await self._startup_complete.wait()
            if not self.backend_ready:
                raise RuntimeError("RAVEN could not start. Check the backend error above.")
            if path.casefold() == "/conversations":
                await chat.mount(ConversationList(self.client.list_conversations()))
                status = "Choose a conversation · Titles and scope identify each chat"
            elif path.casefold() == "/conversations/create":
                self._knowledge_names = [item["name"] for item in await self.client.list_knowledges()]
                for previous in self.query(CreateConversationForm):
                    await previous.remove()
                form = CreateConversationForm(self._knowledge_names)
                await chat.mount(form)
                status = "Global by default · Optionally choose a knowledge · No models needed to create it"
            else:
                await self.display_conversation(path.split("/")[2])
                status = "Conversation opened · Ask a question · Retrieval: auto"
            self.query_one("#status", Static).update(status)
        except Exception as error:
            await chat.mount(ChatMessage("Conversation error", str(error)))
            self.query_one("#status", Static).update("Conversation action failed · Previous selection is unchanged")
        finally:
            self._backend_busy = False
            if form is not None:
                self.call_after_refresh(lambda: form.query_one(Select).scroll_visible(animate=False))
            else:
                chat.scroll_end(animate=False)


    async def display_conversation(self, conversation_id: str) -> None:
        """Read first; only replace the active view when loading succeeds."""
        assert self.client is not None
        opened = await self.client.open_conversation(conversation_id)
        chat = self.query_one("#chat", VerticalScroll)
        await chat.query(".chat-message").remove()
        await chat.mount(ConversationHistory(opened["metadata"], opened["messages"], opened.get("turns")))
        self.active_conversation = opened["conversation"]
        self.conversation_knowledge = opened["metadata"]["knowledge_name"]
        self.update_conversation_header(opened["metadata"])
        self.query_one("#message", PromptEditor).focus()


    @on(Button.Pressed, ".source-button")
    async def view_response_sources(self, event: Button.Pressed) -> None:
        event.stop()
        if isinstance(event.button, SourceButton):
            await self.show_sources(event.button)


    async def show_sources(self, reference: SourceButton) -> None:
        if self.client is None:
            return
        if self._backend_busy:
            self.notify("Wait for the current backend action to finish before opening sources.")
            return
        await self.push_screen(SourceViewer(self.client, reference))


    @on(CreateConversationForm.Submitted)
    def create_conversation(self, event: CreateConversationForm.Submitted) -> None:
        event.stop()
        if not self.backend_ready or self._backend_busy:
            event.form.show_error("Please wait for RAVEN to finish its current action.")
            return
        self._backend_busy = True
        event.form.set_busy(True)
        event.form.show_error("")
        self.query_one("#status", Static).update("Creating conversation…")
        self.run_worker(self.save_conversation(event), name="Create conversation", group="backend")


    async def save_conversation(self, event: CreateConversationForm.Submitted) -> None:
        assert self.client is not None
        try:
            metadata = await self.client.create_conversation(event.knowledge_name)
        except Exception as error:
            event.form.show_error(str(error))
            self.query_one("#status", Static).update("Conversation creation failed · Try again or cancel")
        else:
            await event.form.remove()
            try:
                await self.display_conversation(metadata["conversation_id"])
                self.query_one("#status", Static).update("Conversation created and opened · Configure models, then ask a question")
            except Exception as error:
                await self.query_one("#chat", VerticalScroll).mount(ChatMessage(
                    "Open error", f"Conversation {metadata['conversation_id']} was created, "
                    f"but could not be opened: {error}. Use /conversations to try again.",
                ))
                self.query_one("#status", Static).update("Conversation saved · Opening failed; previous selection is unchanged")
        finally:
            self._backend_busy = False
            if event.form.is_attached:
                event.form.set_busy(False)
            if self.is_running:
                self.query_one("#chat", VerticalScroll).scroll_end(animate=False)


    async def show_knowledge_route(self, path: str) -> None:
        assert self.client is not None
        chat = self.query_one("#chat", VerticalScroll)
        try:
            await self._startup_complete.wait()
            if not self.backend_ready:
                raise RuntimeError("RAVEN could not start. Check the backend error above.")
            if path.casefold() == "/knowledges":
                knowledges = await self.client.list_knowledges()
                self._knowledge_names = [item["name"] for item in knowledges]
                await chat.mount(KnowledgeList(knowledges))
            else:
                details = await self.client.knowledge_details(path.split("/")[2])
                await chat.mount(KnowledgeDetails(details))
            self.query_one("#status", Static).update("Ready · Knowledge view only; conversation scope is unchanged")
        except Exception as error:
            await chat.mount(ChatMessage("Knowledge error", str(error)))
            self.query_one("#status", Static).update("Knowledge action failed · You can try another command")
        finally:
            self._backend_busy = False
            chat.scroll_end(animate=False)


    async def show_model_route(self, path: str) -> None:
        assert self.client is not None
        chat = self.query_one("#chat", VerticalScroll)
        try:
            await self._startup_complete.wait()
            if not self.backend_ready:
                raise RuntimeError("RAVEN could not start. Check the backend error above.")
            if path.casefold() == "/models/configure":
                for form in self.query(ModelConfigurationForm):
                    await form.remove()
                await chat.mount(ModelConfigurationForm(self.client.configured_models()))
                status = "Choose both model roles · API key references are environment-variable names"
            elif path.casefold() == "/providers/ollama/models":
                await chat.mount(OllamaModels(await self.client.list_ollama_models()))
                status = "Installed models listed · No models were loaded"
            else:
                await chat.mount(ConfiguredModels(self.client.configured_models()))
                status = "Configured models · Use /models/configure to change them"
            self.query_one("#status", Static).update(status)
        except Exception as error:
            await chat.mount(ChatMessage("Model error", str(error)))
            self.query_one("#status", Static).update("Model action failed · You can try again")
        finally:
            self._backend_busy = False
            if path.casefold() == "/models/configure" and self.query(ModelConfigurationForm):
                first_input = self.query_one(ModelConfigurationForm).query_one("#llm-provider")
                self.call_after_refresh(lambda: first_input.scroll_visible(animate=False))
            else:
                chat.scroll_end(animate=False)


    @on(ModelConfigurationForm.Submitted)
    def configure_models(self, event: ModelConfigurationForm.Submitted) -> None:
        event.stop()
        if not self.backend_ready or self._backend_busy:
            event.form.show_error("Please wait for RAVEN to finish its current action.")
            return
        self._backend_busy = True
        event.form.set_busy(True)
        event.form.show_error("")
        self.run_worker(self.save_model_configuration(event), name="Configure models", group="backend")


    async def save_model_configuration(self, event: ModelConfigurationForm.Submitted) -> None:
        assert self.client is not None
        chat = self.query_one("#chat", VerticalScroll)
        progress = ModelProgress()
        try:
            await chat.mount(progress)
            self.query_one("#status", Static).update("Configuring models… · Ctrl+C closes the demo and cancels the operation")

            async def show_progress(key: str, text: str) -> None:
                progress.update_progress(key, text)
                self.query_one("#status", Static).update(text)

            await self.client.configure_models(
                event.specs["llm"], event.specs["embedding"], show_progress,
            )
            progress.update_progress("done", "Model pair configured successfully.")
            await event.form.remove()
            await chat.mount(ConfiguredModels(self.client.configured_models()))
            self.query_one("#status", Static).update("Models configured · Attach a document or use /knowledges/<name>/ingest")
            self.query_one("#message", PromptEditor).focus()
        except Exception as error:
            progress.update_progress("failed", f"Configuration failed: {error}")
            event.form.show_error(str(error))
            self.query_one("#status", Static).update("Model configuration failed · Edit the form or cancel")
        finally:
            self._backend_busy = False
            if event.form.is_mounted:
                event.form.set_busy(False)
            chat.scroll_end(animate=False)


    @on(CreateKnowledgeForm.Submitted)
    def create_knowledge(self, event: CreateKnowledgeForm.Submitted) -> None:
        event.stop()
        if not self.backend_ready or self._backend_busy:
            event.form.show_error("Please wait for RAVEN to finish its current action.")
            return
        self._backend_busy = True
        event.form.set_busy(True)
        event.form.show_error("")
        self.query_one("#status", Static).update(f"Creating {event.name}…")
        self.run_worker(self.save_knowledge(event), name="Create knowledge", group="backend")


    async def save_knowledge(self, event: CreateKnowledgeForm.Submitted) -> None:
        assert self.client is not None
        chat = self.query_one("#chat", VerticalScroll)
        try:
            metadata = await self.client.create_knowledge(event.name, event.summary)
            name = metadata["name"]
            self._knowledge_names = [item["name"] for item in await self.client.list_knowledges()]
            details = await self.client.knowledge_details(name)
            await event.form.remove()
            await chat.mount(ChatMessage("Created", f"Knowledge '{name}' was saved."))
            await chat.mount(KnowledgeDetails(details))
            self.query_one("#status", Static).update("Knowledge created · /knowledges to browse")
            self.query_one("#message", PromptEditor).focus()
        except Exception as error:
            event.form.show_error(str(error))
            self.query_one("#status", Static).update("Knowledge creation failed · Edit the name or cancel")
        finally:
            self._backend_busy = False
            if event.form.is_mounted:
                event.form.set_busy(False)
            chat.scroll_end(animate=False)


    @on(Button.Pressed, "#ingest")
    async def attach_document(self) -> None:
        self.dismiss_command_menu()
        if self.client is None:
            self.notify("Backend disabled for this UI preview.", title="Demo")
            self.query_one("#message", PromptEditor).focus()
            return
        self.query_one("#welcome").display = False
        await self.request_ingestion_form(self.conversation_knowledge)


    async def request_ingestion_form(self, knowledge_name: str | None) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        if self._backend_busy:
            await chat.mount(ChatMessage("Please wait", "A backend action is still running."))
        else:
            self._backend_busy = True
            self.query_one("#status", Static).update("Preparing ingestion form…")
            self.run_worker(self.prepare_ingestion_form(knowledge_name), name="Prepare ingestion", group="backend")
        chat.scroll_end(animate=False)


    async def prepare_ingestion_form(self, knowledge_name: str | None) -> None:
        assert self.client is not None
        chat = self.query_one("#chat", VerticalScroll)
        form = None
        try:
            await self._startup_complete.wait()
            if not self.backend_ready:
                raise RuntimeError("RAVEN could not start. Check the backend error above.")
            self._knowledge_names = [item["name"] for item in await self.client.list_knowledges()]
            if not self._knowledge_names:
                raise ValueError("No knowledges yet. Use /knowledges/create before attaching a document.")
            if knowledge_name is not None and knowledge_name not in self._knowledge_names:
                raise ValueError(f"Knowledge '{knowledge_name}' does not exist.")
            if not self.client.models_ready:
                raise ValueError("Configure both models with /models/configure before ingestion.")
            for previous in self.query(IngestionForm):
                await previous.remove()
            form = IngestionForm(self._knowledge_names, knowledge_name)
            await chat.mount(form)
            self.query_one("#status", Static).update("Choose the target knowledge and local file · Enter or click Ingest")
        except Exception as error:
            await chat.mount(ChatMessage("Cannot ingest", str(error)))
            self.query_one("#status", Static).update("Ingestion needs a knowledge and configured models")
        finally:
            self._backend_busy = False
            if form is not None:
                target = form.query_one("#ingestion-path" if knowledge_name is not None else "#ingestion-knowledge")
                self.call_after_refresh(lambda: target.scroll_visible(animate=False))
            else:
                chat.scroll_end(animate=False)


    @on(IngestionForm.Submitted)
    def ingest_document(self, event: IngestionForm.Submitted) -> None:
        event.stop()
        assert self.client is not None
        if self._backend_busy:
            event.form.show_error("Please wait for the current backend action to finish.")
            return
        if not self.client.models_ready:
            event.form.show_error("Configure both models with /models/configure before ingestion.")
            return
        self._backend_busy = True
        event.form.set_busy(True)
        event.form.show_error("")
        self._ingestion_worker = self.run_worker(self.save_ingestion(event), name="Ingest document", group="backend")


    async def save_ingestion(self, event: IngestionForm.Submitted) -> None:
        assert self.client is not None
        chat = self.query_one("#chat", VerticalScroll)
        progress = IngestionProgress()
        try:
            await chat.mount(progress)
            chat.scroll_end(animate=False)
            self.query_one("#status", Static).update("Ingestion starting… · Stop ingestion to cancel")

            async def show_progress(stage: str, text: str) -> None:
                progress.update_progress(stage, text)
                self.query_one("#status", Static).update(text)

            result = await self.client.ingest(event.knowledge_name, event.source_path, show_progress)
        except asyncio.CancelledError:
            if self.is_running and progress.is_attached:
                progress.finish("Ingestion stopped. The file list below reflects any completed writes.")
                self.query_one("#status", Static).update("Ingestion stopped · You can edit the form and try again")
                await self.refresh_ingestion_details(event.knowledge_name)
            raise
        except Exception as error:
            if self.is_running and progress.is_attached:
                progress.finish(f"Ingestion failed: {error}")
                event.form.show_error(str(error))
                self.query_one("#status", Static).update("Ingestion failed · Edit the path or cancel")
        else:
            progress.finish(
                f"Ingested {result['file']} into {event.knowledge_name} · "
                f"{result['section_count']} sections · {result['chunk_count']} chunks"
            )
            await event.form.remove()
            refreshed = await self.refresh_ingestion_details(event.knowledge_name)
            self.query_one("#status", Static).update(
                "Ingestion completed · File details refreshed below" if refreshed
                else "Ingestion completed · File refresh failed; use /knowledges to check"
            )
            self.query_one("#message", PromptEditor).focus()
        finally:
            self._backend_busy = False
            self._ingestion_worker = None
            if event.form.is_attached:
                event.form.set_busy(False)
            if progress.is_attached:
                progress.query_one(".ingestion-spinner").display = False
                progress.query_one(".stop-ingestion", Button).disabled = True
            if self.is_running:
                chat.scroll_end(animate=False)


    async def refresh_ingestion_details(self, knowledge_name: str) -> bool:
        assert self.client is not None
        chat = self.query_one("#chat", VerticalScroll)
        try:
            details = await self.client.knowledge_details(knowledge_name)
        except Exception as error:
            await chat.mount(ChatMessage("Refresh error", f"Could not refresh file details: {error}. Use /knowledges/{knowledge_name} to check."))
            return False
        else:
            # Refresh earlier views too, so their counts/files do not remain stale.
            for view in tuple(self.query(KnowledgeDetails)):
                if view.metadata["name"] == knowledge_name:
                    view.metadata, view.files = details["metadata"], details["files"]
                    await view.recompose()
            for view in tuple(self.query(KnowledgeList)):
                view.knowledges = [
                    {**item, "file_count": len(details["files"])} if item["name"] == knowledge_name else item
                    for item in view.knowledges
                ]
                await view.recompose()
            await chat.mount(KnowledgeDetails(details))
            return True


    @on(Button.Pressed, ".stop-ingestion")
    def stop_ingestion(self, event: Button.Pressed) -> None:
        event.stop()
        if self._ingestion_worker is not None:
            event.button.disabled = True
            self.query_one("#status", Static).update("Stopping ingestion… · Waiting for safe cleanup")
            self._ingestion_worker.cancel()


    def action_focus_message(self) -> None:
        self.query_one("#message", PromptEditor).focus()



if __name__ == "__main__":
    RavenDemo().run()
