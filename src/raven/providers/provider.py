"""Provider facade for Raven model adapters."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from typing import Any

from ..core.errors import ErrorCode, RavenError
from ..core.operations import Operation, OperationManager, OperationTask
from .litellm_manager import LiteLLMManager
from .model_spec import ModelRole, ModelSpec
from .ollama_manager import OllamaManager


class Provider:
    """Route model specifications to the appropriate provider manager."""

    def __init__(
        self,
        *,
        operation_manager: OperationManager,
        ollama_manager: OllamaManager | None = None,
        litellm_manager: LiteLLMManager | None = None,
    ) -> None:
        self._operation_manager = operation_manager
        self.ollama = ollama_manager or OllamaManager(
            operation_manager=operation_manager,
        )
        self.litellm = litellm_manager or LiteLLMManager(
            operation_manager=operation_manager,
        )


    async def load(
        self,
        spec: ModelSpec,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        if spec.role == ModelRole.LLM:
            return await self.load_llm(spec, operation=operation)
        return await self.load_embedding(spec, operation=operation)


    async def load_llm(
        self,
        spec: ModelSpec,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        self._reject_secret_options(spec)
        if spec.role != ModelRole.LLM:
            raise RavenError(
                ErrorCode.INVALID_MODEL_SPEC,
                "load_llm requires a model specification with role='llm'.",
            )
        if spec.provider == "ollama":
            self._reject_ollama_options(spec)
            return await self.ollama.load_llm(
                spec.model,
                operation=operation,
            )
        return await self.litellm.load_llm(
            spec.model,
            provider=spec.provider,
            api_key=self._resolve_api_key(spec),
            options=spec.options,
            operation=operation,
        )


    async def load_embedding(
        self,
        spec: ModelSpec,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        self._reject_secret_options(spec)
        if spec.role != ModelRole.EMBEDDING:
            raise RavenError(
                ErrorCode.INVALID_MODEL_SPEC,
                "load_embedding requires a model specification with role='embedding'.",
            )
        if spec.provider == "ollama":
            self._reject_ollama_options(spec)
            return await self.ollama.load_embedding(
                spec.model,
                operation=operation,
            )
        return await self.litellm.load_embedding(
            spec.model,
            provider=spec.provider,
            api_key=self._resolve_api_key(spec),
            options=spec.options,
            operation=operation,
        )


    async def close(self) -> None:
        """Release provider clients and cached adapters owned by Raven."""
        results = await asyncio.gather(
            self.ollama.close(),
            self.litellm.close(),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise RavenError(
                ErrorCode.MODEL_PROVIDER_FAILED,
                "One or more model providers could not be closed cleanly.",
                details={"failure_count": len(failures)},
            ) from failures[0]


    @staticmethod
    def _reject_ollama_options(spec: ModelSpec) -> None:
        if spec.options:
            raise RavenError(
                ErrorCode.INVALID_MODEL_SPEC,
                "Ollama model options are not supported by this provider interface.",
                details={"fields": sorted(spec.options)},
            )


    @staticmethod
    def _reject_secret_options(spec: ModelSpec) -> None:
        credential_fields = {
            "access_token",
            "api_key",
            "apikey",
            "api_token",
            "auth_token",
            "authorization",
            "bearer_token",
            "client_secret",
            "credential",
            "credentials",
            "password",
            "refresh_token",
            "secret",
            "secret_key",
            "session_token",
            "token",
        }
        credential_suffixes = (
            "_access_key",
            "_access_token",
            "_api_key",
            "_auth_token",
            "_password",
            "_secret_key",
        )
        fields: list[str] = []

        def inspect(value: Any, path: str = "") -> None:
            if isinstance(value, Mapping):
                for raw_key, nested in value.items():
                    key = str(raw_key)
                    field_path = f"{path}.{key}" if path else key
                    normalized = key.strip().lower().replace("-", "_")
                    if (
                        normalized in credential_fields
                        or normalized.endswith(credential_suffixes)
                    ):
                        fields.append(field_path)
                    else:
                        inspect(nested, field_path)
            elif isinstance(value, Sequence) and not isinstance(
                value,
                (str, bytes, bytearray),
            ):
                for index, nested in enumerate(value):
                    inspect(nested, f"{path}[{index}]")

        inspect(spec.options)
        fields.sort()
        if fields:
            raise RavenError(
                ErrorCode.INVALID_MODEL_SPEC,
                "Credentials must be supplied through api_key_ref, not model options.",
                details={"fields": fields},
            )


    @staticmethod
    def _resolve_api_key(spec: ModelSpec) -> str | None:
        if spec.api_key_ref is None:
            return None
        api_key = os.getenv(spec.api_key_ref)
        if not api_key:
            raise RavenError(
                ErrorCode.MODEL_API_KEY_NOT_FOUND,
                f"Environment variable '{spec.api_key_ref}' was not found.",
                details={
                    "provider": spec.provider,
                    "model": spec.model,
                    "api_key_ref": spec.api_key_ref,
                },
            )
        return api_key
