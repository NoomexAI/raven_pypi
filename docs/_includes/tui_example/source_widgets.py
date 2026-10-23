"""Per-turn source actions and a plain-text reconstructed-file viewer."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, LoadingIndicator, Select, Static
from textual.worker import Worker, WorkerCancelled

if TYPE_CHECKING:
    from raven_client import RavenClient



class SourceButton(Button):

    def __init__(
        self,
        conversation_id: str,
        turn_id: str,
        cached_files: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__("View sources", classes="source-button", compact=True)
        self.conversation_id = conversation_id
        self.turn_id = turn_id
        self.cached_files = cached_files



class SourceSection(Vertical):

    def __init__(self, section: dict[str, Any], navigation_type: str) -> None:
        super().__init__(classes="source-section source-hit" if section["highlighted"] else "source-section")
        self.section = section
        self.navigation_type = navigation_type


    def compose(self) -> ComposeResult:
        label = "SUPPLIED TO MODEL" if self.section["highlighted"] else "Surrounding context"
        location = self.section.get("source_range")
        if location is not None:
            label += f" · {self.navigation_type}: {location[0]}–{location[1]}"
        yield Static(label, classes="source-section-label", markup=False)
        yield Static(self.section["section_id"], classes="list-hint", markup=False)
        yield Static(self.section.get("raw_content") or "(No text content)", markup=False)



class SourceViewer(ModalScreen[None]):
    """Read saved snapshots first; rebuilding never reruns the model or retrieval."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, client: RavenClient, reference: SourceButton) -> None:
        super().__init__()
        self.client = client
        self.reference = reference
        self.files: list[dict[str, Any]] = []
        self._load_worker: Worker[None] | None = None
        self._shown_index: int | None = None


    def compose(self) -> ComposeResult:
        with Vertical(id="source-dialog"):
            yield Static("Reconstructed sources", classes="message-speaker")
            yield Static(
                f"Conversation: {self.reference.conversation_id} · Turn: {self.reference.turn_id}",
                classes="list-hint source-reference", markup=False,
            )
            yield Static(
                "Highlighted sections were supplied to the model; they do not guarantee its answer is correct.",
                classes="list-hint source-warning",
            )
            yield Select([], prompt="Choose a file", classes="source-file-choice")
            yield Static("", id="source-provenance", classes="list-hint", markup=False)
            with VerticalScroll(id="source-document"):
                yield Static("Loading sources…", classes="list-hint")
            yield LoadingIndicator(id="source-spinner")
            yield Static("Loading saved reconstruction…", id="source-status", markup=False)
            with Horizontal(classes="form-actions"):
                yield Button("Rebuild from saved evidence", id="rebuild-sources", compact=True)
                yield Button("Close", id="close-sources", compact=True)


    def on_mount(self) -> None:
        self.on_resize()
        self.query_one(".source-file-choice").display = False
        self.query_one("#close-sources").focus()
        self.start_loading()


    def on_resize(self) -> None:
        self.set_class(self.size.height < 26, "compact-height")


    def start_loading(self, *, rebuild: bool = False) -> None:
        if self._load_worker is not None and not self._load_worker.is_finished:
            return
        self.query_one("#source-spinner").display = True
        self.query_one("#rebuild-sources", Button).disabled = True
        self.query_one(Select).disabled = True
        self._load_worker = self.run_worker(self.load_sources(rebuild=rebuild), name="View turn sources")


    async def load_sources(self, *, rebuild: bool) -> None:
        try:
            async def progress(text: str) -> None:
                self.query_one("#source-status", Static).update(text)

            if not rebuild and self.reference.cached_files:
                loaded = {"files": self.reference.cached_files, "snapshot": True}
            else:
                loaded = await self.client.turn_sources(
                    self.reference.conversation_id, self.reference.turn_id,
                    rebuild=rebuild, on_progress=progress,
                )
            self.files = loaded["files"]
            self._shown_index = None
            selector = self.query_one(Select)
            selector.display = bool(self.files)
            selector.set_options([
                (Text(f"{file['knowledge_name']} / {file['file_name']}"), index)
                for index, file in enumerate(self.files)
            ])
            self.query_one("#source-provenance", Static).update(
                "Saved snapshot from this turn. Rebuild reads the current knowledge contents."
                if loaded["snapshot"] else
                "Rebuilt from saved evidence using current knowledge contents; not a new model response."
            )
            if self.files:
                selector.value = 0
                await self.show_file(0)
            else:
                document = self.query_one("#source-document", VerticalScroll)
                await document.remove_children()
                await document.mount(Static("This turn has no reconstructable section evidence."))
            self.query_one("#source-status", Static).update(f"{len(self.files)} reconstructed file(s) · Esc to close")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.is_attached:
                self.query_one("#source-status", Static).update(f"Could not load/rebuild sources: {error}")
                if not self.files:
                    document = self.query_one("#source-document", VerticalScroll)
                    await document.remove_children()
                    await document.mount(Static("No sources loaded. You can retry rebuilding, or close this view."))
        finally:
            if self.is_attached:
                self.query_one("#source-spinner").display = False
                self.query_one("#rebuild-sources", Button).disabled = False
                self.query_one(Select).disabled = not bool(self.files)


    @on(Select.Changed, ".source-file-choice")
    async def select_file(self, event: Select.Changed) -> None:
        event.stop()
        if isinstance(event.value, int) and 0 <= event.value < len(self.files):
            await self.show_file(event.value)


    async def show_file(self, index: int) -> None:
        if self._shown_index == index:
            return
        self._shown_index = index
        file = self.files[index]
        document = self.query_one("#source-document", VerticalScroll)
        await document.remove_children()
        await document.mount(*(
            SourceSection(section, file.get("navigation_type", "none"))
            for section in file["sections"]
        ))
        document.scroll_home(animate=False)


    @on(Button.Pressed, "#rebuild-sources")
    def rebuild(self) -> None:
        self.start_loading(rebuild=True)


    @on(Button.Pressed, "#close-sources")
    async def action_close(self) -> None:
        await self.stop_loading()
        self.dismiss()


    async def stop_loading(self) -> None:
        if self._load_worker is not None and not self._load_worker.is_finished:
            self._load_worker.cancel()
            try:
                await self._load_worker.wait()
            except WorkerCancelled:
                pass


    async def on_unmount(self) -> None:
        await self.stop_loading()
