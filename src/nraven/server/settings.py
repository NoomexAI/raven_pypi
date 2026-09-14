"""Server bootstrap helpers built on Raven's core configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..core.config import SystemConfig, load_system_config, resolve_raven_home


def resolve_server_configuration(
    raven_home: str | Path | None = None,
    *,
    system_settings_path: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, SystemConfig]:
    """Resolve the bootstrap path and load its immutable system settings."""
    resolved_home = resolve_raven_home(raven_home, environment=environment)
    config = load_system_config(resolved_home, system_settings_path)
    return resolved_home, config
