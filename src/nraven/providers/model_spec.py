"""Provider-neutral model configuration."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelRole(StrEnum):
    """The Raven capability provided by a model."""

    LLM = "llm"
    EMBEDDING = "embedding"




class ModelSpec(BaseModel):
    """Describe a model without coupling Raven to its provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    role: ModelRole
    api_key_ref: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("model provider and model name cannot be empty")
        return value.lower()

    @field_validator("model")
    @classmethod
    def _strip_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("model provider and model name cannot be empty")
        return value
