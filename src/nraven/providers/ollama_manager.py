"""Ollama model access for Raven.

Ollama is an external service.  This module owns Raven's interaction with that
service, but it does not start or stop the Ollama process.
"""

from __future__ import annotations

import asyncio
import math
import os
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import ollama
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.llms.ollama import Ollama

from ..core.config import (
    DEFAULT_EMBEDDING_ADAPTER_CACHE_SIZE,
    DEFAULT_LLM_ADAPTER_CACHE_SIZE,
)
from ..core.errors import ErrorCode, RavenError, error_payload
from ..core.events import Event, EventType
from ..core.operations import Operation, OperationManager, OperationTask, OperationType

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass(slots=True)
class _KeyedLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0



class OllamaManager:
    """Manage Ollama models and create their LlamaIndex adapters."""

    _RESIDENCY_POLL_INTERVAL_SECONDS = 0.1
    _RESIDENCY_CONFIRMATION_TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        host: str | None = None,
        request_timeout: float = 300.0,
        embedding_batch_size: int = 10,
        *,
        operation_manager: OperationManager,
        max_cached_llms: int = DEFAULT_LLM_ADAPTER_CACHE_SIZE,
        max_cached_embeddings: int = DEFAULT_EMBEDDING_ADAPTER_CACHE_SIZE,
    ) -> None:

        self.host = host or os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
        self.request_timeout = request_timeout
        self.embedding_batch_size = embedding_batch_size

        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if embedding_batch_size <= 0:
            raise ValueError("embedding_batch_size must be positive")
        self._validate_cache_size(max_cached_llms, "LLM")
        self._validate_cache_size(max_cached_embeddings, "embedding")

        self._client = ollama.AsyncClient(
            host=self.host,
            timeout=self.request_timeout,
        )
        self._max_cached_llms = max_cached_llms
        self._max_cached_embeddings = max_cached_embeddings
        self._llms: OrderedDict[str, Ollama] = OrderedDict()
        self._embeddings: OrderedDict[str, OllamaEmbedding] = OrderedDict()
        self._model_locks: dict[str, _KeyedLock] = {}
        self._pull_locks: dict[str, _KeyedLock] = {}
        self._closed = False
        self._operation_manager = operation_manager


    async def check_connection(self, *, operation: Operation | None = None) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.MODEL_CHECK_CONNECTION
        )
        return await active_operation.run(
            OperationType.MODEL_CHECK_CONNECTION,
            lambda active_operation: self._check_connection(operation=active_operation),
        )


    async def _check_connection(self, *, operation: Operation) -> None:
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


    async def list_models(self, *, operation: Operation | None = None) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.MODEL_LIST
        )
        return await active_operation.run(
            OperationType.MODEL_LIST,
            lambda active_operation: self._list_models_with_events(
                operation=active_operation,
            ),
        )


    async def _list_models_with_events(
        self,
        *,
        operation: Operation,
    ) -> list[dict[str, Any]]:
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


    async def inspect(self, model: str, *, operation: Operation | None = None) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.MODEL_INSPECT
        )
        return await active_operation.run(
            OperationType.MODEL_INSPECT,
            lambda active_operation: self._inspect(
                model,
                operation=active_operation,
            ),
        )


    async def _inspect(
        self,
        model: str,
        *,
        operation: Operation,
    ) -> dict[str, Any]:
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
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.MODEL_PULL
        )
        return await active_operation.run(
            OperationType.MODEL_PULL,
            lambda active_operation: self._pull(
                model,
                on_progress,
                operation=active_operation,
            ),
        )


    async def _pull(
        self,
        model: str,
        on_progress: ProgressCallback | None = None,
        *,
        operation: Operation,
    ) -> None:
        """Pull a model, forwarding each progress item to the caller."""
        async with self._pull_guard(model):
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


    async def delete(self, model: str, *, operation: Operation | None = None) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.MODEL_DELETE
        )
        return await active_operation.run(
            OperationType.MODEL_DELETE,
            lambda active_operation: self._delete(
                model,
                operation=active_operation,
            ),
        )


    async def _delete(self, model: str, *, operation: Operation) -> None:
        await self._emit(operation, EventType.MODEL_DELETE_STARTED, {"model": model})
        try:
            async with self._model_guard(model), self._pull_guard(model):
                await self._client.delete(model)
                self._llms.pop(model, None)
                self._embeddings.pop(model, None)
        except Exception as exc:
            error = self._provider_error("delete model", model)
            await self._emit(
                operation,
                EventType.MODEL_DELETE_FAILED,
                {"model": model, "error": error_payload(error)},
            )
            raise error from exc
        await self._emit(operation, EventType.MODEL_DELETE_COMPLETED, {"model": model})


    async def load_llm(self, model: str, *, operation: Operation | None = None) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.MODEL_LOAD_LLM
        )
        return await active_operation.run(
            OperationType.MODEL_LOAD_LLM,
            lambda active_operation: self._load_llm(
                model,
                operation=active_operation,
            ),
        )


    async def _load_llm(
        self,
        model: str,
        *,
        operation: Operation,
    ) -> Ollama:
        """Return a LlamaIndex LLM adapter, pulling the model if necessary."""
        await self._emit(operation, EventType.MODEL_LOAD_LLM_STARTED, {"model": model})
        try:
            async with self._model_guard(model):
                await self.ensure_available(model, operation=operation)
                cached = model in self._llms
                if cached:
                    self._llms.move_to_end(model)
                    adapter = self._llms[model]
                else:
                    self._llms[model] = Ollama(
                        model=model,
                        base_url=self.host,
                        request_timeout=self.request_timeout,
                    )
                    self._trim_cache(self._llms, self._max_cached_llms)
                    adapter = self._llms[model]
        except Exception as exc:
            error = self._provider_error("load LLM", model)
            await self._emit(
                operation,
                EventType.MODEL_LOAD_LLM_FAILED,
                {"model": model, "error": error_payload(error)},
            )
            raise error from exc
        await self._emit(operation, EventType.MODEL_LOAD_LLM_COMPLETED, {"model": model, "cached": cached})
        return adapter


    async def load_embedding(
        self,
        model: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.MODEL_LOAD_EMBEDDING
        )
        return await active_operation.run(
            OperationType.MODEL_LOAD_EMBEDDING,
            lambda active_operation: self._load_embedding(
                model,
                operation=active_operation,
            ),
        )


    async def _load_embedding(
        self,
        model: str,
        *,
        operation: Operation,
    ) -> OllamaEmbedding:
        """Return a LlamaIndex embedding adapter, pulling the model if necessary."""
        await self._emit(operation, EventType.MODEL_LOAD_EMBEDDING_STARTED, {"model": model})
        try:
            async with self._model_guard(model):
                await self.ensure_available(model, operation=operation)
                cached = model in self._embeddings
                if cached:
                    self._embeddings.move_to_end(model)
                    adapter = self._embeddings[model]
                else:
                    self._embeddings[model] = OllamaEmbedding(
                        model_name=model,
                        base_url=self.host,
                        embed_batch_size=self.embedding_batch_size,
                        client_kwargs={"timeout": self.request_timeout},
                    )
                    self._trim_cache(
                        self._embeddings,
                        self._max_cached_embeddings,
                    )
                    adapter = self._embeddings[model]
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
        return adapter


    async def ensure_available(
        self,
        model: str,
        *,
        operation: Operation | None = None,
    ) -> None:
        installed = {item.get("model") or item.get("name") for item in await self._list_models()}
        if model not in installed:
            pull_task = await self.pull(model, operation=operation)
            await pull_task.result()


    async def preload_llm(
        self,
        model: str,
        keep_alive: str | float | None = None,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        self._validate_keep_alive(keep_alive)
        active_operation = operation or await self._operation_manager.create(
            OperationType.MODEL_PRELOAD_LLM
        )
        return await active_operation.run(
            OperationType.MODEL_PRELOAD_LLM,
            lambda active_operation: self._preload_llm(
                model,
                keep_alive,
                operation=active_operation,
            ),
        )


    async def _preload_llm(
        self,
        model: str,
        keep_alive: str | float | None,
        *,
        operation: Operation,
    ) -> None:
        data = self._residency_event_data(model, "llm", keep_alive)
        await self._emit(operation, EventType.MODEL_PRELOAD_LLM_STARTED, data)
        try:
            async with self._model_guard(model):
                await self.ensure_available(model, operation=operation)
                await self._client.generate(
                    model=model,
                    prompt="",
                    stream=False,
                    keep_alive=keep_alive,
                )
                await self._wait_for_residency(model, resident=True)
        except Exception as exc:
            error = self._provider_error("preload LLM", model)
            await self._emit(
                operation,
                EventType.MODEL_PRELOAD_LLM_FAILED,
                {**data, "error": error_payload(error)},
            )
            raise error from exc
        await self._emit(operation, EventType.MODEL_PRELOAD_LLM_COMPLETED, data)


    async def preload_embedding(
        self,
        model: str,
        keep_alive: str | float | None = None,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        self._validate_keep_alive(keep_alive)
        active_operation = operation or await self._operation_manager.create(
            OperationType.MODEL_PRELOAD_EMBEDDING
        )
        return await active_operation.run(
            OperationType.MODEL_PRELOAD_EMBEDDING,
            lambda active_operation: self._preload_embedding(
                model,
                keep_alive,
                operation=active_operation,
            ),
        )


    async def _preload_embedding(
        self,
        model: str,
        keep_alive: str | float | None,
        *,
        operation: Operation,
    ) -> None:
        data = self._residency_event_data(model, "embedding", keep_alive)
        await self._emit(operation, EventType.MODEL_PRELOAD_EMBEDDING_STARTED, data)
        try:
            async with self._model_guard(model):
                await self.ensure_available(model, operation=operation)
                await self._client.embed(
                    model=model,
                    input="",
                    keep_alive=keep_alive,
                )
                await self._wait_for_residency(model, resident=True)
        except Exception as exc:
            error = self._provider_error("preload embedding model", model)
            await self._emit(
                operation,
                EventType.MODEL_PRELOAD_EMBEDDING_FAILED,
                {**data, "error": error_payload(error)},
            )
            raise error from exc
        await self._emit(
            operation,
            EventType.MODEL_PRELOAD_EMBEDDING_COMPLETED,
            data,
        )


    async def unload_llm(self, model: str, *, operation: Operation | None = None) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.MODEL_UNLOAD_LLM
        )
        return await active_operation.run(
            OperationType.MODEL_UNLOAD_LLM,
            lambda active_operation: self._unload_llm(
                model,
                operation=active_operation,
            ),
        )


    async def _unload_llm(self, model: str, *, operation: Operation) -> None:
        await self._emit(operation, EventType.MODEL_UNLOAD_LLM_STARTED, {"model": model})
        try:
            async with self._model_guard(model):
                await self._client.generate(
                    model=model,
                    prompt="",
                    keep_alive=0,
                    options={"num_predict": 1},
                )
                await self._wait_for_residency(model, resident=False)
                self._llms.pop(model, None)
        except Exception as exc:
            error = self._provider_error("unload LLM", model)
            await self._emit(
                operation,
                EventType.MODEL_UNLOAD_LLM_FAILED,
                {"model": model, "error": error_payload(error)},
            )
            raise error from exc
        await self._emit(operation, EventType.MODEL_UNLOAD_LLM_COMPLETED, {"model": model})


    async def unload_embedding(
        self,
        model: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.MODEL_UNLOAD_EMBEDDING
        )
        return await active_operation.run(
            OperationType.MODEL_UNLOAD_EMBEDDING,
            lambda active_operation: self._unload_embedding(
                model,
                operation=active_operation,
            ),
        )


    async def _unload_embedding(
        self,
        model: str,
        *,
        operation: Operation,
    ) -> None:
        await self._emit(operation, EventType.MODEL_UNLOAD_EMBEDDING_STARTED, {"model": model})
        try:
            async with self._model_guard(model):
                await self._client.embed(model=model, input="", keep_alive=0)
                await self._wait_for_residency(model, resident=False)
                self._embeddings.pop(model, None)
        except Exception as exc:
            error = self._provider_error("unload embedding model", model)
            await self._emit(
                operation,
                EventType.MODEL_UNLOAD_EMBEDDING_FAILED,
                {"model": model, "error": error_payload(error)},
            )
            raise error from exc
        await self._emit(operation, EventType.MODEL_UNLOAD_EMBEDDING_COMPLETED, {"model": model})


    async def close(self) -> None:
        """Close Raven's client without stopping the external Ollama service."""
        if self._closed:
            return
        self._closed = True
        self._llms.clear()
        self._embeddings.clear()
        self._model_locks.clear()
        self._pull_locks.clear()
        await self._client.close()


    def _model_guard(self, model: str) -> AbstractAsyncContextManager[None]:
        return self._keyed_lock(self._model_locks, model)


    def _pull_guard(self, model: str) -> AbstractAsyncContextManager[None]:
        return self._keyed_lock(self._pull_locks, model)


    async def _wait_for_residency(self, model: str, *, resident: bool) -> None:
        timeout = min(
            self.request_timeout,
            self._RESIDENCY_CONFIRMATION_TIMEOUT_SECONDS,
        )
        deadline = asyncio.get_running_loop().time() + timeout
        expected_name = self._canonical_model_name(model)
        while True:
            response = await self._client.ps()
            models = response.models if hasattr(response, "models") else response["models"]
            running = {
                self._canonical_model_name(
                    str(
                        getattr(item, "model", None)
                        or getattr(item, "name", None)
                        or item.get("model")
                        or item.get("name")
                    )
                )
                for item in models
            }
            if (expected_name in running) == resident:
                return
            if asyncio.get_running_loop().time() >= deadline:
                state = "resident" if resident else "unloaded"
                raise TimeoutError(
                    f"Ollama model '{model}' was not confirmed as {state}."
                )
            await asyncio.sleep(self._RESIDENCY_POLL_INTERVAL_SECONDS)


    @staticmethod
    @asynccontextmanager
    async def _keyed_lock(
        registry: dict[str, _KeyedLock],
        key: str,
    ) -> AsyncGenerator[None, None]:
        entry = registry.get(key)
        if entry is None:
            entry = _KeyedLock()
            registry[key] = entry
        entry.users += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            entry.users -= 1
            if entry.users == 0 and registry.get(key) is entry:
                registry.pop(key, None)


    @staticmethod
    def _trim_cache(cache: OrderedDict[str, Any], maximum: int) -> None:
        while len(cache) > maximum:
            cache.popitem(last=False)


    @staticmethod
    def _canonical_model_name(model: str) -> str:
        final_segment = model.rsplit("/", 1)[-1]
        return model if ":" in final_segment else f"{model}:latest"


    @staticmethod
    def _validate_cache_size(value: int, role: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RavenError(
                ErrorCode.INVALID_RESOURCE_CACHE_SIZE,
                f"{role} adapter cache size must be a positive integer.",
            )


    @staticmethod
    def _validate_keep_alive(value: str | float | None) -> None:
        if value is None:
            return
        if isinstance(value, bool):
            raise RavenError(
                ErrorCode.INVALID_MODEL_SPEC,
                "Ollama keep_alive must be a duration string or finite number.",
            )
        if isinstance(value, str):
            if value.strip() != value or not value:
                raise RavenError(
                    ErrorCode.INVALID_MODEL_SPEC,
                    "Ollama keep_alive duration cannot be empty or padded.",
                )
            if value == "0":
                raise RavenError(
                    ErrorCode.INVALID_MODEL_SPEC,
                    "Ollama preload keep_alive cannot be zero; use unload instead.",
                )
            return
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise RavenError(
                ErrorCode.INVALID_MODEL_SPEC,
                "Ollama keep_alive must be a duration string or finite number.",
            )
        if value == 0:
            raise RavenError(
                ErrorCode.INVALID_MODEL_SPEC,
                "Ollama preload keep_alive cannot be zero; use unload instead.",
            )


    @staticmethod
    def _residency_event_data(
        model: str,
        role: str,
        keep_alive: str | float | None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"model": model, "role": role}
        if keep_alive is not None:
            data["keep_alive"] = keep_alive
        return data


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
