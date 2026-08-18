"""Ollama model access for Raven.

Ollama is an external service.  This module owns Raven's interaction with that
service, but it does not start or stop the Ollama process.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

import ollama
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.llms.ollama import Ollama


ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class ModelManager:
    """Manage Ollama models and create their LlamaIndex adapters."""

    def __init__(
        self,
        host: str | None = None,
        request_timeout: float = 300.0,
        embedding_batch_size: int = 10,
    ) -> None:
        
        self.host = host or os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
        self.request_timeout = request_timeout
        self.embedding_batch_size = embedding_batch_size

        self._client = ollama.AsyncClient(host=self.host)
        self._llms: dict[str, Ollama] = {}
        self._embeddings: dict[str, OllamaEmbedding] = {}

    async def check_connection(self) -> None:
        """Raise the Ollama client error if the configured server is unavailable."""
        await self._client.list()

    async def list_models(self) -> list[dict[str, Any]]:
        response = await self._client.list()
        models = response.models if hasattr(response, "models") else response["models"]
        return [self._dump(model) for model in models]

    async def inspect(self, model: str) -> dict[str, Any]:
        response = await self._client.show(model)
        return self._dump(response)

    async def pull(self, model: str, on_progress: ProgressCallback | None = None) -> None:
        """Pull a model, forwarding each progress item to the caller."""
        stream = await self._client.pull(model, stream=True)
        async for item in stream:
            if on_progress is None:
                continue
            progress = self._dump(item)
            result = on_progress(progress)
            if result is not None:
                await result

    async def delete(self, model: str) -> None:
        await self._client.delete(model)
        self._llms.pop(model, None)
        self._embeddings.pop(model, None)

    async def load_llm(self, model: str) -> Ollama:
        """Return a LlamaIndex LLM adapter, pulling the model if necessary."""
        await self.ensure_available(model)
        if model not in self._llms:
            self._llms[model] = Ollama(
                model=model,
                base_url=self.host,
                request_timeout=self.request_timeout,
            )
        return self._llms[model]

    async def load_embedding(self, model: str) -> OllamaEmbedding:
        """Return a LlamaIndex embedding adapter, pulling the model if necessary."""
        await self.ensure_available(model)
        if model not in self._embeddings:
            self._embeddings[model] = OllamaEmbedding(
                model_name=model,
                base_url=self.host,
                embed_batch_size=self.embedding_batch_size,
            )
        return self._embeddings[model]

    async def ensure_available(self, model: str) -> None:
        installed = {item.get("model") or item.get("name") for item in await self.list_models()}
        if model not in installed:
            await self.pull(model)

    async def unload_llm(self, model: str) -> None:
        await self._client.generate(model=model, prompt="", keep_alive=0, options={"num_predict": 1})
        self._llms.pop(model, None)

    async def unload_embedding(self, model: str) -> None:
        await self._client.embed(model=model, input="", keep_alive=0)
        self._embeddings.pop(model, None)

    @staticmethod
    def _dump(value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            return value.model_dump()
        return dict(value)
