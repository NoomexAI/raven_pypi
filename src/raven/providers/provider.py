"""Provider facade for Raven model adapters."""

from __future__ import annotations

import asyncio
import os

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
        secret_markers = ("api_key", "token", "secret", "password", "authorization")
        fields = sorted(
            key
            for key in spec.options
            if any(marker in key.lower() for marker in secret_markers)
        )
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
