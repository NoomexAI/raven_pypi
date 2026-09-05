"""LlamaIndex LiteLLM adapters for cloud and OpenAI-compatible providers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..core.errors import ErrorCode, RavenError, error_payload
from ..core.events import Event, EventType
from ..core.operations import Operation, OperationManager, OperationTask, OperationType


class LiteLLMManager:
    """Create and cache LlamaIndex adapters backed by LiteLLM."""

    def __init__(self, *, operation_manager: OperationManager) -> None:
        self._operation_manager = operation_manager
        self._llms: dict[str, Any] = {}
        self._embeddings: dict[str, Any] = {}


    async def load_llm(
        self,
        model: str,
        *,
        provider: str | None = None,
        api_key: str | None = None,
        options: dict[str, Any] | None = None,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.MODEL_LOAD_LLM
        )
        return await active_operation.run(
            OperationType.MODEL_LOAD_LLM,
            lambda active_operation: self._load_llm(
                model,
                provider=provider,
                api_key=api_key,
                options=options,
                operation=active_operation,
            ),
        )


    async def _load_llm(
        self,
        model: str,
        *,
        provider: str | None,
        api_key: str | None,
        options: dict[str, Any] | None,
        operation: Operation,
    ) -> Any:
        data = self._event_data(model=model, provider=provider, role="llm")
        await self._emit(operation, EventType.MODEL_LOAD_LLM_STARTED, data)
        try:
            from llama_index.llms.litellm import LiteLLM

            model_options = dict(options or {})
            self._reject_reserved_options(
                model_options,
                {"model", "api_key", "custom_llm_provider"},
            )
            cache_key = self._cache_key(
                role="llm",
                model=model,
                provider=provider,
                api_key=api_key,
                options=model_options,
            )
            cached = cache_key in self._llms
            if not cached:
                llm_options: dict[str, Any] = {
                    "model": model,
                    "api_key": api_key,
                    **model_options,
                }
                if provider is not None:
                    llm_options["custom_llm_provider"] = provider
                self._llms[cache_key] = LiteLLM(**llm_options)
        except RavenError as error:
            await self._emit(
                operation,
                EventType.MODEL_LOAD_LLM_FAILED,
                {**data, "error": error_payload(error)},
            )
            raise
        except Exception as exc:
            error = self._provider_error(
                model=model,
                provider=provider,
                role="llm",
                action="load LLM",
            )
            await self._emit(
                operation,
                EventType.MODEL_LOAD_LLM_FAILED,
                {**data, "error": error_payload(error)},
            )
            raise error from exc

        await self._emit(
            operation,
            EventType.MODEL_LOAD_LLM_COMPLETED,
            {**data, "cached": cached},
        )
        return self._llms[cache_key]


    async def load_embedding(
        self,
        model: str,
        *,
        api_key: str | None = None,
        options: dict[str, Any] | None = None,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.MODEL_LOAD_EMBEDDING
        )
        return await active_operation.run(
            OperationType.MODEL_LOAD_EMBEDDING,
            lambda active_operation: self._load_embedding(
                model,
                api_key=api_key,
                options=options,
                operation=active_operation,
            ),
        )


    async def _load_embedding(
        self,
        model: str,
        *,
        api_key: str | None,
        options: dict[str, Any] | None,
        operation: Operation,
    ) -> Any:
        data = self._event_data(model=model, provider=None, role="embedding")
        await self._emit(operation, EventType.MODEL_LOAD_EMBEDDING_STARTED, data)
        try:
            from llama_index.embeddings.litellm import LiteLLMEmbedding

            embedding_options = dict(options or {})
            embed_batch_size = embedding_options.pop("embed_batch_size", 10)
            self._reject_unsupported_embedding_options(
                embedding_options,
                {"api_base", "dimensions", "timeout"},
            )
            cache_key = self._cache_key(
                role="embedding",
                model=model,
                provider=None,
                api_key=api_key,
                options={
                    "embed_batch_size": embed_batch_size,
                    **embedding_options,
                },
            )
            cached = cache_key in self._embeddings
            if not cached:
                self._embeddings[cache_key] = LiteLLMEmbedding(
                    model_name=model,
                    api_key=api_key,
                    embed_batch_size=embed_batch_size,
                    **embedding_options,
                )
        except RavenError as error:
            await self._emit(
                operation,
                EventType.MODEL_LOAD_EMBEDDING_FAILED,
                {**data, "error": error_payload(error)},
            )
            raise
        except Exception as exc:
            error = self._provider_error(
                model=model,
                provider=None,
                role="embedding",
                action="load embedding model",
            )
            await self._emit(
                operation,
                EventType.MODEL_LOAD_EMBEDDING_FAILED,
                {**data, "error": error_payload(error)},
            )
            raise error from exc

        await self._emit(
            operation,
            EventType.MODEL_LOAD_EMBEDDING_COMPLETED,
            {**data, "cached": cached},
        )
        return self._embeddings[cache_key]


    @staticmethod
    def _reject_reserved_options(
        options: dict[str, Any],
        reserved: set[str],
    ) -> None:
        invalid = sorted(reserved.intersection(options))
        if invalid:
            raise RavenError(
                ErrorCode.INVALID_MODEL_SPEC,
                "Model options cannot override manager-controlled fields.",
                details={"fields": invalid},
            )


    @staticmethod
    def _reject_unsupported_embedding_options(
        options: dict[str, Any],
        supported: set[str],
    ) -> None:
        unsupported = sorted(set(options).difference(supported))
        if unsupported:
            raise RavenError(
                ErrorCode.INVALID_MODEL_SPEC,
                "Unsupported LiteLLM embedding options were provided.",
                details={"fields": unsupported},
            )


    @staticmethod
    def _event_data(
        *,
        model: str,
        provider: str | None,
        role: str,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"model": model, "role": role}
        if provider is not None:
            data["provider"] = provider
        return data


    @staticmethod
    def _provider_error(
        *,
        model: str,
        provider: str | None,
        role: str,
        action: str,
    ) -> RavenError:
        details: dict[str, Any] = {"model": model, "role": role}
        if provider is not None:
            details["provider"] = provider
        return RavenError(
            ErrorCode.MODEL_PROVIDER_FAILED,
            f"Unable to {action} through LiteLLM.",
            details=details,
        )


    @staticmethod
    def _cache_key(
        *,
        role: str,
        model: str,
        provider: str | None,
        api_key: str | None,
        options: dict[str, Any],
    ) -> str:
        key_fingerprint = (
            hashlib.sha256(api_key.encode()).hexdigest() if api_key else None
        )
        return json.dumps(
            {
                "role": role,
                "model": model,
                "provider": provider,
                "api_key": key_fingerprint,
                "options": options,
            },
            sort_keys=True,
            default=repr,
        )


    @staticmethod
    async def _emit(
        operation: Operation,
        event_type: EventType,
        data: dict[str, Any],
    ) -> None:
        await operation.publish(Event(type=event_type, data=data))
