from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import ollama
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.llms.ollama import Ollama

from .events import Event, EventBus, EventType
from .local_ollama import LocalOllama

DEFAULT_EMBED_MODEL = os.environ.get("RAVEN_EMBED_MODEL", "bge-m3")
DEFAULT_BASE_MODEL = os.environ.get("RAVEN_BASE_MODEL", "gemma4:e2b")

EMBED_BATCH_SIZE = 10
LLM_TIMEOUT = 300.0


def _terminal_progress(done: int, total: int) -> None:
    if total > 0:
        pct = done / total * 100
        width = 30
        filled = int(width * done / total)
        bar = "#" * filled + "-" * (width - filled)
        sys.stdout.write(f"\r[{bar}] {pct:5.1f}% {done / 2**20:8.1f} / {total / 2**20:8.1f} MiB")
    else:
        sys.stdout.write(f"\r{done / 2**20:8.1f} MiB")
    sys.stdout.flush()


class ModelManager:
    """Sole owner of the Ollama server and of all model lifecycle.

    ONLY this class touches LocalOllama, model names, or constructs
    llama_index Ollama/OllamaEmbedding clients. Other components receive the
    *loaded* model objects via ``load_embed_model()`` / ``load_base_model()``
    and use them; nobody else can pull/delete/load/unload a model.
    """

    def __init__(
        self,
        bus: EventBus,
        server: LocalOllama | None = None,
    ) -> None:
        self._bus = bus
        self.server = server or LocalOllama()
        self._client = ollama.AsyncClient(host=self.server.base_url)
        self._loaded_embed: OllamaEmbedding | None = None
        self._loaded_base: Ollama | None = None

    @property
    def base_url(self) -> str:
        return self.server.base_url

    async def start(
        self,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        op_id = uuid4().hex
        bar = on_progress or _terminal_progress

        def publish_progress(done: int, total: int) -> None:
            self._bus.publish_from_thread(
                Event(
                    type=EventType.OLLAMA_DOWNLOAD_PROGRESS,
                    data={"bytes_done": done, "bytes_total": total},
                    op_id=op_id,
                )
            )
            bar(done, total)

        downloaded = await self.server.start(on_progress=publish_progress)
        if downloaded:
            self._bus.publish(
                Event(
                    type=EventType.OLLAMA_DOWNLOAD_COMPLETE,
                    data={"binary": str(self.server.bin_path)},
                    op_id=op_id,
                )
            )
            sys.stdout.write("\n")
            sys.stdout.flush()

    async def stop(self) -> None:
        self._loaded_embed = None
        self._loaded_base = None
        await self.server.stop()

    async def list_models(self) -> list[dict[str, Any]]:
        resp = await self._client.list()
        return [m.model_dump() for m in resp["models"]]

    async def inspect(self, model: str) -> dict[str, Any]:
        resp = await self._client.show(model)
        return resp.model_dump()

    async def pull(self, model: str, op_id: str | None = None) -> str:
        op_id = op_id or uuid4().hex
        asyncio.create_task(self._pull_task(model, op_id))
        return op_id

    async def _pull_task(self, model: str, op_id: str) -> None:
        try:
            stream = await self._client.pull(model, stream=True)
            async for progress in stream:
                self._bus.publish(
                    Event(
                        type=EventType.MODEL_PULL_PROGRESS,
                        data={"model": model, **progress.model_dump()},
                        op_id=op_id,
                    )
                )
            self._bus.publish(
                Event(
                    type=EventType.MODEL_PULL_COMPLETE,
                    data={"model": model},
                    op_id=op_id,
                )
            )
        except Exception as exc:
            self._bus.publish(
                Event(
                    type=EventType.ERROR,
                    data={"model": model, "error": str(exc)},
                    op_id=op_id,
                )
            )

    async def delete(self, model: str) -> None:
        """Delete a model from the local store (single owner of model lifecycle)."""
        await self._client.delete(model)
        self._bus.publish(Event(type=EventType.MODEL_DELETED, data={"model": model}))

    async def _ensure_available(self, model: str) -> None:
        """Pull the model if it is not already installed locally."""
        installed = {m["model"] for m in await self.list_models()}
        if model in installed:
            return
        op_id = await self.pull(model)
        for ev in self._bus.history(op_id=op_id):
            if ev.type is EventType.MODEL_PULL_COMPLETE:
                return
            if ev.type is EventType.ERROR:
                raise RuntimeError(f"failed to pull model '{model}': {ev.data.get('error')}")
        async for ev in self._bus.subscribe(op_id=op_id):
            if ev.type is EventType.MODEL_PULL_COMPLETE:
                return
            if ev.type is EventType.ERROR:
                raise RuntimeError(f"failed to pull model '{model}': {ev.data.get('error')}")

    async def load_embed_model(self, model: str | None = None) -> OllamaEmbedding:
        """Return a loaded OllamaEmbedding for ``model`` (default embed model). Cached."""
        name = model or DEFAULT_EMBED_MODEL
        if self._loaded_embed is not None and self._loaded_embed.model_name == name:
            return self._loaded_embed
        await self._ensure_available(name)
        self._loaded_embed = OllamaEmbedding(
            model_name=name,
            base_url=self.server.base_url,
            embed_batch_size=EMBED_BATCH_SIZE,
        )
        return self._loaded_embed

    async def load_base_model(self, model: str | None = None) -> Ollama:
        """Return a loaded Ollama LLM for ``model`` (default base model). Cached."""
        name = model or DEFAULT_BASE_MODEL
        if self._loaded_base is not None and self._loaded_base.model == name:
            return self._loaded_base
        await self._ensure_available(name)
        self._loaded_base = Ollama(model=name, base_url=self.server.base_url, request_timeout=LLM_TIMEOUT)
        return self._loaded_base

    async def unload_embed_model(self, model: str | None = None) -> None:
        """Free ``model`` from VRAM (keep_alive=0) and drop the cached handle."""
        name = model or DEFAULT_EMBED_MODEL
        await self._unload(name, kind="embed")
        if self._loaded_embed is not None and self._loaded_embed.model_name == name:
            self._loaded_embed = None

    async def unload_base_model(self, model: str | None = None) -> None:
        name = model or DEFAULT_BASE_MODEL
        await self._unload(name, kind="generate")
        if self._loaded_base is not None and self._loaded_base.model == name:
            self._loaded_base = None

    async def _unload(self, model: str, kind: str = "generate") -> None:
        try:
            # keep_alive=0 makes the runner exit and release its VRAM.
            # Embed models are unloaded via /api/embed (they don't support
            # /api/generate); generation models via /api/generate.
            if kind == "embed":
                await self._client.embed(model=model, input="", keep_alive=0)
            else:
                await self._client.generate(
                    model=model, prompt="", keep_alive=0, options={"num_predict": 1}
                )
        finally:
            self._bus.publish(Event(type=EventType.MODEL_UNLOADED, data={"model": model}))
