"""Static Raven paths derived from one configurable home directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PathConfig:
    """Expose Raven's fixed directory layout under ``raven_home``."""
    raven_home: Path


    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "raven_home",
            Path(self.raven_home).expanduser().resolve(),
        )


    @property
    def knowledge_base_dir(self) -> Path:
        return self.raven_home / "data" / "knowledge_base"


    @property
    def event_storage_dir(self) -> Path:
        return self.raven_home / "events"


    @property
    def conversations_dir(self) -> Path:
        return self.raven_home / "data" / "conversations"


    @property
    def uploads_dir(self) -> Path:
        return self.raven_home / "temp" / "uploads"
