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

from .events import Event, EventType
from .errors import ErrorCode, RavenError, error_payload
from .operations import Operation

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class OllamaManager:
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


    async def check_connection(self, *, operation: Operation | None = None) -> None:
        """Raise the Ollama client error if the configured server is unavailable."""
        await self._emit(operation, EventType.MODEL_CONNECTION_STARTED)
        try:
            await self._client.list()
        except Exception as exc:
            error = self._provider_error("connect")
            await self._emit(
                operation,
                EventType.MODEL_CONNECTION_FAILED,
                {"error": error_payload(error)},
            )
            raise error from exc
        await self._emit(operation, EventType.MODEL_CONNECTION_COMPLETED)


    async def list_models(self, *, operation: Operation | None = None) -> list[dict[str, Any]]:
        await self._emit(operation, EventType.MODEL_LIST_STARTED)
        try:
            models = await self._list_models()
        except Exception as exc:
            error = self._provider_error("list models")
            await self._emit(
                operation,
                EventType.MODEL_LIST_FAILED,
                {"error": error_payload(error)},
            )
            raise error from exc
        await self._emit(operation, EventType.MODEL_LIST_COMPLETED, {"count": len(models)})
        return models


    async def _list_models(self) -> list[dict[str, Any]]:
        response = await self._client.list()
        models = response.models if hasattr(response, "models") else response["models"]
        return [self._dump(model) for model in models]


    async def inspect(self, model: str, *, operation: Operation | None = None) -> dict[str, Any]:
        await self._emit(operation, EventType.MODEL_INSPECT_STARTED, {"model": model})
        try:
            response = await self._client.show(model)
            inspected = self._dump(response)
        except Exception as exc:
            error = self._provider_error("inspect model", model)
            await self._emit(
                operation,
                EventType.MODEL_INSPECT_FAILED,
                {"model": model, "error": error_payload(error)},
            )
            raise error from exc
        await self._emit(operation, EventType.MODEL_INSPECT_COMPLETED, {"model": model})
        return inspected


    async def pull(
        self,
        model: str,
        on_progress: ProgressCallback | None = None,
        *,
        operation: Operation | None = None,
    ) -> None:
        """Pull a model, forwarding each progress item to the caller."""
        await self._emit(operation, EventType.MODEL_PULL_STARTED, {"model": model})
        try:
            stream = await self._client.pull(model, stream=True)
            async for item in stream:
                progress = self._dump(item)
                await self._emit(
                    operation,
                    EventType.MODEL_PULL_PROGRESS,
                    {"model": model, **progress},
                )
                if on_progress is not None:
                    result = on_progress(progress)
                    if result is not None:
                        await result
        except Exception as exc:
            error = self._provider_error("pull model", model)
            await self._emit(
                operation,
                EventType.MODEL_PULL_FAILED,
                {"model": model, "error": error_payload(error)},
            )
            raise error from exc
        await self._emit(operation, EventType.MODEL_PULL_COMPLETED, {"model": model})


    async def delete(self, model: str, *, operation: Operation | None = None) -> None:
        await self._emit(operation, EventType.MODEL_DELETE_STARTED, {"model": model})
        try:
            await self._client.delete(model)
        except Exception as exc:
            error = self._provider_error("delete model", model)
            await self._emit(
                operation,
                EventType.MODEL_DELETE_FAILED,
                {"model": model, "error": error_payload(error)},
            )
            raise error from exc
        self._llms.pop(model, None)
        self._embeddings.pop(model, None)
        await self._emit(operation, EventType.MODEL_DELETE_COMPLETED, {"model": model})


    async def load_llm(self, model: str, *, operation: Operation | None = None) -> Ollama:
        """Return a LlamaIndex LLM adapter, pulling the model if necessary."""
        await self._emit(operation, EventType.MODEL_LOAD_LLM_STARTED, {"model": model})
        try:
            await self.ensure_available(model, operation=operation)
            cached = model in self._llms
            if not cached:
                self._llms[model] = Ollama(
                    model=model,
                    base_url=self.host,
                    request_timeout=self.request_timeout,
                )
        except Exception as exc:
            error = self._provider_error("load LLM", model)
            await self._emit(
                operation,
                EventType.MODEL_LOAD_LLM_FAILED,
                {"model": model, "error": error_payload(error)},
            )
            raise error from exc
        await self._emit(operation, EventType.MODEL_LOAD_LLM_COMPLETED, {"model": model, "cached": cached})
        return self._llms[model]


    async def load_embedding(
        self,
        model: str,
        *,
        operation: Operation | None = None,
    ) -> OllamaEmbedding:
        """Return a LlamaIndex embedding adapter, pulling the model if necessary."""
        await self._emit(operation, EventType.MODEL_LOAD_EMBEDDING_STARTED, {"model": model})
        try:
            await self.ensure_available(model, operation=operation)
            cached = model in self._embeddings
            if not cached:
                self._embeddings[model] = OllamaEmbedding(
                    model_name=model,
                    base_url=self.host,
                    embed_batch_size=self.embedding_batch_size,
                )
        except Exception as exc:
            error = self._provider_error("load embedding model", model)
            await self._emit(
                operation,
                EventType.MODEL_LOAD_EMBEDDING_FAILED,
                {"model": model, "error": error_payload(error)},
            )
            raise error from exc
        await self._emit(
            operation,
            EventType.MODEL_LOAD_EMBEDDING_COMPLETED,
            {"model": model, "cached": cached},
        )
        return self._embeddings[model]


    async def ensure_available(
        self,
        model: str,
        *,
        operation: Operation | None = None,
    ) -> None:
        installed = {item.get("model") or item.get("name") for item in await self._list_models()}
        if model not in installed:
            await self.pull(model, operation=operation)

    async def unload_llm(self, model: str, *, operation: Operation | None = None) -> None:
        await self._emit(operation, EventType.MODEL_UNLOAD_LLM_STARTED, {"model": model})
        try:
            await self._client.generate(
                model=model,
                prompt="",
                keep_alive=0,
                options={"num_predict": 1},
            )
        except Exception as exc:
            error = self._provider_error("unload LLM", model)
            await self._emit(
                operation,
                EventType.MODEL_UNLOAD_LLM_FAILED,
                {"model": model, "error": error_payload(error)},
            )
            raise error from exc
        self._llms.pop(model, None)
        await self._emit(operation, EventType.MODEL_UNLOAD_LLM_COMPLETED, {"model": model})


    async def unload_embedding(
        self,
        model: str,
        *,
        operation: Operation | None = None,
    ) -> None:
        await self._emit(operation, EventType.MODEL_UNLOAD_EMBEDDING_STARTED, {"model": model})
        try:
            await self._client.embed(model=model, input="", keep_alive=0)
        except Exception as exc:
            error = self._provider_error("unload embedding model", model)
            await self._emit(
                operation,
                EventType.MODEL_UNLOAD_EMBEDDING_FAILED,
                {"model": model, "error": error_payload(error)},
            )
            raise error from exc
        self._embeddings.pop(model, None)
        await self._emit(operation, EventType.MODEL_UNLOAD_EMBEDDING_COMPLETED, {"model": model})


    @staticmethod
    async def _emit(
        operation: Operation | None,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> None:
        if operation is not None:
            await operation.publish(Event(type=event_type, data=data or {}))


    @staticmethod
    def _provider_error(action: str, model: str | None = None) -> RavenError:
        details: dict[str, Any] = {"action": action}
        if model is not None:
            details["model"] = model

        if action == "connect":
            return RavenError(
                ErrorCode.OLLAMA_UNAVAILABLE,
                "Ollama is unavailable.",
                details=details,
            )
        return RavenError(
            ErrorCode.OLLAMA_OPERATION_FAILED,
            f"Unable to {action} through Ollama.",
            details=details,
        )


    @staticmethod
    def _dump(value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            return value.model_dump()
        return dict(value)
