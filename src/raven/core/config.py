"""Static Raven paths derived from one configurable home directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


DEFAULT_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
EVENT_SYNC_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class PathConfig:
    """Expose Raven's fixed directory layout for one user."""

    raven_home: Path
    user_id: UUID | str = DEFAULT_USER_ID


    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "raven_home",
            Path(self.raven_home).expanduser().resolve(),
        )
        try:
            normalized_user_id = UUID(str(self.user_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("user_id must be a valid UUID.") from exc
        object.__setattr__(self, "user_id", normalized_user_id)


    @property
    def user_root(self) -> Path:
        return self.raven_home / str(self.user_id)


    @property
    def knowledge_base_dir(self) -> Path:
        return self.user_root / "data" / "knowledge_base"


    @property
    def event_storage_dir(self) -> Path:
        return self.user_root / "events"


    @property
    def event_database_path(self) -> Path:
        return self.event_storage_dir / "events.sqlite3"


    @property
    def conversations_dir(self) -> Path:
        return self.user_root / "data" / "conversations"


    @property
    def uploads_dir(self) -> Path:
        return self.user_root / "temp" / "uploads"
