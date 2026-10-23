"""Inline model configuration and compact model activity views."""

from __future__ import annotations

import re
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Input, Static



class ConfiguredModels(Vertical):

    def __init__(self, models: dict[str, Any]) -> None:
        super().__init__(classes="chat-message model-view")
        self.models = models


    def compose(self) -> ComposeResult:
        yield Static("Configured models", classes="message-speaker")
        for role, label in (("llm", "LLM"), ("embedding", "Embedding")):
            model = self.models.get(role)
            text = f"{model['provider']} / {model['model']}" if model else "Not configured"
            yield Static(f"{label}: {text}", markup=False)
        yield Static(
            "Use /models/configure to choose the pair. These are configured identities, "
            "not a live VRAM monitor.", classes="list-hint",
        )



class OllamaModels(Vertical):

    def __init__(self, models: list[dict[str, Any]]) -> None:
        super().__init__(classes="chat-message model-view")
        self.models = models


    def compose(self) -> ComposeResult:
        yield Static("Installed Ollama models", classes="message-speaker")
        if not self.models:
            yield Static("No Ollama models installed. Configure can download a named model when needed.")
        for model in self.models:
            yield Static(str(model.get("model") or model.get("name") or "Unknown model"), markup=False)
        yield Static("Use the exact model name in /models/configure.", classes="list-hint")



class ModelProgress(Vertical):

    def __init__(self) -> None:
        super().__init__(classes="chat-message model-view")
        self.entries: list[tuple[str, str]] = []


    def compose(self) -> ComposeResult:
        yield Static("Model configuration", classes="message-speaker")
        yield Static("Preparing…", classes="model-progress", markup=False)


    def update_progress(self, key: str, text: str) -> None:
        if self.entries and self.entries[-1][0] == key:
            self.entries[-1] = (key, text)
        else:
            self.entries.append((key, text))
            self.entries = self.entries[-8:]
        self.query_one(".model-progress", Static).update("\n".join(text for _, text in self.entries))



class ModelConfigurationForm(Vertical):
    """Two model roles; API key inputs are environment-variable references only."""

    class Submitted(Message):

        def __init__(self, form: ModelConfigurationForm, specs: dict[str, dict[str, Any]]) -> None:
            super().__init__()
            self.form = form
            self.specs = specs


    def __init__(self, models: dict[str, Any]) -> None:
        super().__init__(classes="chat-message model-form")
        self.models = models


    def compose(self) -> ComposeResult:
        yield Static("Configure models", classes="message-speaker")
        yield Static(
            "Configure prepares both models, downloads missing Ollama models, and loads them. "
            "Cloud models use remote inference, not local VRAM.", classes="list-hint",
        )
        with Horizontal(classes="model-columns"):
            for role, label, example in (("llm", "LLM", "gemma4:e2b"), ("embedding", "Embedding", "bge-m3:latest")):
                current = self.models.get(role) or {}
                with Vertical(classes="model-role"):
                    yield Static(label, classes="message-speaker")
                    yield Static("Provider", classes="field-label")
                    yield Input(value=current.get("provider", "ollama"), id=f"{role}-provider")
                    yield Static("Model name", classes="field-label")
                    yield Input(value=current.get("model", ""), placeholder=example, id=f"{role}-model")
                    yield Static("API key reference · optional", classes="field-label")
                    yield Input(placeholder="YOUR_API_KEY", id=f"{role}-key-ref")
                    yield Static(
                        "Environment-variable name only. Never paste an API key.",
                        classes="list-hint api-key-hint",
                    )
        yield Static(
            "Keep the embedding model unchanged after ingestion. Different embedding models "
            "cannot reuse each other's vectors.", classes="list-hint",
        )
        yield Static("", classes="form-error", markup=False)
        with Horizontal(classes="form-actions"):
            yield Button("Configure", classes="configure-models", compact=True)
            yield Button("Cancel", classes="cancel-models", compact=True)


    def on_mount(self) -> None:
        self.query_one("#llm-provider", Input).focus()


    @on(Input.Submitted)
    def next_field(self, event: Input.Submitted) -> None:
        event.stop()
        inputs = list(self.query(Input))
        index = inputs.index(event.input)
        if index + 1 < len(inputs):
            inputs[index + 1].focus()
        else:
            self.submit()


    @on(Button.Pressed, ".configure-models")
    def submit(self) -> None:
        specs: dict[str, dict[str, Any]] = {}
        for role in ("llm", "embedding"):
            provider = self.query_one(f"#{role}-provider", Input).value.strip().lower()
            model = self.query_one(f"#{role}-model", Input).value.strip()
            key_ref = self.query_one(f"#{role}-key-ref", Input).value.strip()
            if not provider or not model:
                self.show_error(f"Provide a provider and model name for {role}.")
                return
            if key_ref and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_ref):
                self.show_error("API key reference must be an environment-variable name, not the key itself.")
                return
            specs[role] = {"provider": provider, "model": model, "role": role, "api_key_ref": key_ref or None}
        self.post_message(self.Submitted(self, specs))


    @on(Button.Pressed, ".cancel-models")
    async def cancel(self, event: Button.Pressed) -> None:
        event.stop()
        await self.remove()
        self.app.query_one("#message").focus()


    def set_busy(self, busy: bool) -> None:
        for widget in self.query("Input, Button"):
            widget.disabled = busy


    def show_error(self, message: str) -> None:
        self.query_one(".form-error", Static).update(message)
