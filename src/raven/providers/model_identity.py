"""Stable, credential-free identities for persisted embedding spaces."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def embedding_identity(embed_model: Any) -> str:
    """Fingerprint the adapter settings that determine its vector space."""
    identity = {
        "adapter": f"{type(embed_model).__module__}.{type(embed_model).__qualname__}",
        "model": _first_string(embed_model, "model_name", "model"),
        "provider": _first_string(embed_model, "custom_llm_provider", "provider"),
        "base_url": _first_string(embed_model, "base_url", "api_base"),
        "dimensions": getattr(embed_model, "dimensions", None),
        "query_instruction": getattr(embed_model, "query_instruction", None),
        "text_instruction": getattr(embed_model, "text_instruction", None),
    }
    serialized = json.dumps(identity, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _first_string(value: Any, *names: str) -> str | None:
    for name in names:
        candidate = getattr(value, name, None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None
