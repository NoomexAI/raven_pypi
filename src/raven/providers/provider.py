"""Provider facade for Raven model adapters."""

from __future__ import annotations

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
        if spec.provider == "ollama":
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
        if spec.provider == "ollama":
            return await self.ollama.load_embedding(
                spec.model,
                operation=operation,
            )
        return await self.litellm.load_embedding(
            spec.model,
            api_key=self._resolve_api_key(spec),
            options=spec.options,
            operation=operation,
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
