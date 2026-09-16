"""Validated Raven configuration and user-scoped path derivation."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from ipaddress import IPv6Address, ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .errors import ErrorCode, RavenError


DEFAULT_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
SYSTEM_CONFIG_SCHEMA_VERSION = 1
RUNTIME_CONFIG_SCHEMA_VERSION = 1
OPERATION_SYNC_INTERVAL_SECONDS = 1.0
DEFAULT_EVENT_REPLAY_PAGE_SIZE = 256
DEFAULT_OPERATION_PAGE_SIZE = 50
DEFAULT_OPERATION_CLEANUP_BATCH_SIZE = 100
DEFAULT_FINISHED_OPERATION_CACHE_SIZE = 256
DEFAULT_OPEN_KNOWLEDGE_LIMIT = 16
DEFAULT_OPEN_CONVERSATION_LIMIT = 64
DEFAULT_LLM_ADAPTER_CACHE_SIZE = 8
DEFAULT_EMBEDDING_ADAPTER_CACHE_SIZE = 8
DEFAULT_PROMPT_CACHE_SIZE = 32
DEFAULT_MAX_SOURCE_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_UPLOAD_REQUEST_OVERHEAD_BYTES = 64 * 1024
DEFAULT_MAX_DOCUMENT_PAGES = 1_000


@dataclass(frozen=True, slots=True)
class SystemConfig:
    """Immutable process-wide behavior and safety limits."""

    schema_version: int = SYSTEM_CONFIG_SCHEMA_VERSION
    host: str = "127.0.0.1"
    port: int = 8765
    log_level: str = "info"
    operation_sync_interval_seconds: float = OPERATION_SYNC_INTERVAL_SECONDS
    operation_retention_seconds: float = 86_400.0
    upload_retry_retention_seconds: float = 86_400.0
    operation_cleanup_interval_seconds: float = 300.0
    operation_cleanup_batch_size: int = DEFAULT_OPERATION_CLEANUP_BATCH_SIZE
    event_replay_page_size: int = DEFAULT_EVENT_REPLAY_PAGE_SIZE
    operation_page_size: int = DEFAULT_OPERATION_PAGE_SIZE
    finished_operation_cache_size: int = DEFAULT_FINISHED_OPERATION_CACHE_SIZE
    open_knowledge_limit: int = DEFAULT_OPEN_KNOWLEDGE_LIMIT
    open_conversation_limit: int = DEFAULT_OPEN_CONVERSATION_LIMIT
    llm_adapter_cache_size: int = DEFAULT_LLM_ADAPTER_CACHE_SIZE
    embedding_adapter_cache_size: int = DEFAULT_EMBEDDING_ADAPTER_CACHE_SIZE
    prompt_cache_size: int = DEFAULT_PROMPT_CACHE_SIZE
    sse_heartbeat_interval_seconds: float = 30.0
    max_source_file_bytes: int = DEFAULT_MAX_SOURCE_FILE_BYTES
    max_upload_request_overhead_bytes: int = DEFAULT_MAX_UPLOAD_REQUEST_OVERHEAD_BYTES
    max_document_pages: int = DEFAULT_MAX_DOCUMENT_PAGES
    max_retrieval_top_k: int = 25
    max_agent_iterations: int = 50
    runtime_idle_seconds: float = 1_800.0
    max_user_runtimes: int = 100
    cors_origins: tuple[str, ...] = ("http://localhost:3000",)
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]")
    require_user_id_header: bool = False
    trusted_ingestion_enabled: bool = False
    allowed_ingestion_roots: tuple[Path, ...] = ()


    def __post_init__(self) -> None:
        _require_schema_version(
            self.schema_version,
            SYSTEM_CONFIG_SCHEMA_VERSION,
            ErrorCode.UNSUPPORTED_SYSTEM_CONFIG_VERSION,
            "system",
        )
        _require_nonempty_string(self.host, "host", ErrorCode.INVALID_SYSTEM_CONFIG)
        _require_int_range(
            self.port,
            "port",
            ErrorCode.INVALID_SYSTEM_CONFIG,
            minimum=1,
            maximum=65_535,
        )
        if (
            not isinstance(self.log_level, str)
            or self.log_level not in {"critical", "error", "warning", "info", "debug"}
        ):
            _invalid_config(
                ErrorCode.INVALID_SYSTEM_CONFIG,
                "log_level",
                "must be one of critical, error, warning, info, or debug",
            )

        for field_name in (
            "operation_sync_interval_seconds",
            "operation_cleanup_interval_seconds",
            "sse_heartbeat_interval_seconds",
            "runtime_idle_seconds",
        ):
            _require_positive_number(
                getattr(self, field_name),
                field_name,
                ErrorCode.INVALID_SYSTEM_CONFIG,
            )
        _require_nonnegative_number(
            self.operation_retention_seconds,
            "operation_retention_seconds",
            ErrorCode.INVALID_SYSTEM_CONFIG,
        )
        _require_positive_number(
            self.upload_retry_retention_seconds,
            "upload_retry_retention_seconds",
            ErrorCode.INVALID_SYSTEM_CONFIG,
        )

        for field_name in (
            "operation_cleanup_batch_size",
            "event_replay_page_size",
            "operation_page_size",
            "open_knowledge_limit",
            "open_conversation_limit",
            "llm_adapter_cache_size",
            "embedding_adapter_cache_size",
            "prompt_cache_size",
            "max_source_file_bytes",
            "max_upload_request_overhead_bytes",
            "max_document_pages",
            "max_retrieval_top_k",
            "max_agent_iterations",
            "max_user_runtimes",
        ):
            _require_positive_int(
                getattr(self, field_name),
                field_name,
                ErrorCode.INVALID_SYSTEM_CONFIG,
            )
        _require_nonnegative_int(
            self.finished_operation_cache_size,
            "finished_operation_cache_size",
            ErrorCode.INVALID_SYSTEM_CONFIG,
        )

        origins = _normalize_origins(self.cors_origins)
        hosts = _normalize_hosts(self.allowed_hosts)
        roots = _normalize_paths(self.allowed_ingestion_roots, "allowed_ingestion_roots")
        for field_name in ("require_user_id_header", "trusted_ingestion_enabled"):
            if not isinstance(getattr(self, field_name), bool):
                _invalid_config(
                    ErrorCode.INVALID_SYSTEM_CONFIG,
                    field_name,
                    "must be a boolean",
                )
        if self.trusted_ingestion_enabled and not roots:
            _invalid_config(
                ErrorCode.INVALID_SYSTEM_CONFIG,
                "allowed_ingestion_roots",
                "must contain at least one path when trusted ingestion is enabled",
            )
        object.__setattr__(self, "cors_origins", origins)
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "allowed_ingestion_roots", roots)


    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation without secrets."""
        return {
            field.name: (
                [str(path) for path in self.allowed_ingestion_roots]
                if field.name == "allowed_ingestion_roots"
                else list(self.cors_origins)
                if field.name == "cors_origins"
                else list(self.allowed_hosts)
                if field.name == "allowed_hosts"
                else getattr(self, field.name)
            )
            for field in fields(self)
        }


    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SystemConfig":
        payload = _strict_payload(value, cls, ErrorCode.INVALID_SYSTEM_CONFIG)
        try:
            return cls(**payload)
        except RavenError:
            raise
        except (TypeError, ValueError) as exc:
            raise RavenError(
                ErrorCode.INVALID_SYSTEM_CONFIG,
                "The system configuration is invalid.",
            ) from exc


    @classmethod
    def load(cls, path: str | Path) -> "SystemConfig":
        return cls.from_dict(_read_json_object(Path(path), "system configuration"))


    def save(self, path: str | Path) -> None:
        _atomic_write_json(Path(path), self.to_dict())




@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Immutable snapshot of one user's adjustable operation defaults."""

    schema_version: int = RUNTIME_CONFIG_SCHEMA_VERSION
    revision: int = 0
    breakpoint_percentile_threshold: int = 95
    buffer_size: int = 1
    max_extraction_retries: int = 3
    chunk_size: int = 512
    chunk_overlap: int = 50
    retrieval_top_k: int = 3
    agent_max_iterations: int = 10
    memory_token_limit: int = 4_000
    memory_top_k: int = 5


    def __post_init__(self) -> None:
        _require_schema_version(
            self.schema_version,
            RUNTIME_CONFIG_SCHEMA_VERSION,
            ErrorCode.UNSUPPORTED_RUNTIME_CONFIG_VERSION,
            "runtime",
        )
        _require_nonnegative_int(
            self.revision,
            "revision",
            ErrorCode.INVALID_RUNTIME_CONFIG,
        )
        _require_int_range(
            self.breakpoint_percentile_threshold,
            "breakpoint_percentile_threshold",
            ErrorCode.INVALID_RUNTIME_CONFIG,
            minimum=1,
            maximum=100,
        )
        for field_name in (
            "buffer_size",
            "max_extraction_retries",
            "chunk_size",
            "retrieval_top_k",
            "agent_max_iterations",
            "memory_token_limit",
            "memory_top_k",
        ):
            _require_positive_int(
                getattr(self, field_name),
                field_name,
                ErrorCode.INVALID_RUNTIME_CONFIG,
            )
        _require_nonnegative_int(
            self.chunk_overlap,
            "chunk_overlap",
            ErrorCode.INVALID_RUNTIME_CONFIG,
        )
        if self.chunk_overlap >= self.chunk_size:
            _invalid_config(
                ErrorCode.INVALID_RUNTIME_CONFIG,
                "chunk_overlap",
                "must be smaller than chunk_size",
            )


    def validate_against(self, system_config: SystemConfig) -> None:
        """Enforce immutable process-wide safety limits."""
        if self.retrieval_top_k > system_config.max_retrieval_top_k:
            _invalid_config(
                ErrorCode.INVALID_RUNTIME_CONFIG,
                "retrieval_top_k",
                f"cannot exceed the system maximum of {system_config.max_retrieval_top_k}",
            )
        if self.agent_max_iterations > system_config.max_agent_iterations:
            _invalid_config(
                ErrorCode.INVALID_RUNTIME_CONFIG,
                "agent_max_iterations",
                f"cannot exceed the system maximum of {system_config.max_agent_iterations}",
            )


    def to_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


    def updated(self, changes: Mapping[str, Any]) -> "RuntimeConfig":
        """Create the next revision from a partial user-provided update."""
        payload = dict(changes)
        immutable = {"schema_version", "revision"}.intersection(payload)
        if immutable:
            _invalid_config(
                ErrorCode.INVALID_RUNTIME_CONFIG,
                sorted(immutable)[0],
                "is managed by Raven and cannot be updated directly",
            )
        allowed = {field.name for field in fields(self)} - {"schema_version", "revision"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            _invalid_config(
                ErrorCode.INVALID_RUNTIME_CONFIG,
                unknown[0],
                "is not a recognized runtime setting",
            )
        try:
            return replace(self, revision=self.revision + 1, **payload)
        except RavenError:
            raise
        except (TypeError, ValueError) as exc:
            raise RavenError(
                ErrorCode.INVALID_RUNTIME_CONFIG,
                "The runtime configuration update is invalid.",
            ) from exc


    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RuntimeConfig":
        payload = _strict_payload(value, cls, ErrorCode.INVALID_RUNTIME_CONFIG)
        try:
            return cls(**payload)
        except RavenError:
            raise
        except (TypeError, ValueError) as exc:
            raise RavenError(
                ErrorCode.INVALID_RUNTIME_CONFIG,
                "The runtime configuration is invalid.",
            ) from exc


    @classmethod
    def load(cls, path: str | Path) -> "RuntimeConfig":
        return cls.from_dict(_read_json_object(Path(path), "runtime configuration"))


    @classmethod
    def load_or_create(
        cls,
        path: str | Path,
        system_config: SystemConfig,
    ) -> "RuntimeConfig":
        settings_path = Path(path)
        if settings_path.exists():
            config = cls.load(settings_path)
        else:
            config = cls()
            config.validate_against(system_config)
            config.save(settings_path)
        config.validate_against(system_config)
        return config


    def save(self, path: str | Path) -> None:
        _atomic_write_json(Path(path), self.to_dict())




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
    def operation_storage_dir(self) -> Path:
        return self.user_root / "operations"


    @property
    def operation_database_path(self) -> Path:
        return self.operation_storage_dir / "operations.sqlite3"


    @property
    def conversations_dir(self) -> Path:
        return self.user_root / "data" / "conversations"


    @property
    def runtime_settings_dir(self) -> Path:
        return self.user_root / "runtime_settings"


    @property
    def runtime_settings_path(self) -> Path:
        return self.runtime_settings_dir / "runtime_settings.json"


    @property
    def uploads_dir(self) -> Path:
        return self.user_root / "temp" / "uploads"




def resolve_raven_home(
    explicit_home: str | Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve CLI value, environment value, then the platform default."""
    if explicit_home is not None:
        return Path(explicit_home).expanduser().resolve()
    env = os.environ if environment is None else environment
    configured = env.get("RAVEN_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return _platform_default_home(env).resolve()


def load_system_config(
    raven_home: str | Path,
    settings_path: str | Path | None = None,
) -> SystemConfig:
    """Load an explicit file or discover/create Raven home's default file."""
    home = Path(raven_home).expanduser().resolve()
    if settings_path is not None:
        selected = Path(settings_path).expanduser().resolve()
        if not selected.is_file():
            raise RavenError(
                ErrorCode.INVALID_SYSTEM_CONFIG,
                "The explicitly selected system configuration file does not exist.",
                details={"path": str(selected)},
            )
        return SystemConfig.load(selected)

    selected = home / "system_settings" / "system_settings.json"
    if selected.exists():
        if not selected.is_file():
            raise RavenError(
                ErrorCode.INVALID_SYSTEM_CONFIG,
                "The default system configuration path is not a file.",
                details={"path": str(selected)},
            )
        return SystemConfig.load(selected)

    config = SystemConfig()
    config.save(selected)
    return config


def _strict_payload(
    value: Mapping[str, Any],
    config_type: type[Any],
    error_code: ErrorCode,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RavenError(error_code, "The configuration must be a JSON object.")
    payload = dict(value)
    allowed = {field.name for field in fields(config_type)}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        _invalid_config(error_code, unknown[0], "is not a recognized setting")
    return payload


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RavenError(
            ErrorCode.PERSISTENCE_FAILED,
            f"The {label} could not be read.",
            details={"path": str(path)},
        ) from exc
    if not isinstance(value, dict):
        raise RavenError(
            ErrorCode.INVALID_SYSTEM_CONFIG
            if label == "system configuration"
            else ErrorCode.INVALID_RUNTIME_CONFIG,
            f"The {label} must be a JSON object.",
        )
    return value


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except (OSError, TypeError, ValueError) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RavenError(
            ErrorCode.PERSISTENCE_FAILED,
            "The configuration could not be persisted.",
            details={"path": str(path)},
        ) from exc


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _platform_default_home(environment: Mapping[str, str]) -> Path:
    if sys.platform == "win32":
        base = environment.get("LOCALAPPDATA")
        return Path(base) / "Raven" if base else Path.home() / "AppData" / "Local" / "Raven"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Raven"
    base = environment.get("XDG_DATA_HOME")
    return Path(base) / "raven" if base else Path.home() / ".local" / "share" / "raven"


def _normalize_origins(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        _invalid_config(
            ErrorCode.INVALID_SYSTEM_CONFIG,
            "cors_origins",
            "must be a list of HTTP origins",
        )
    normalized: list[str] = []
    for origin in value:
        if not isinstance(origin, str):
            _invalid_config(
                ErrorCode.INVALID_SYSTEM_CONFIG,
                "cors_origins",
                "must contain only strings",
            )
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            _invalid_config(
                ErrorCode.INVALID_SYSTEM_CONFIG,
                "cors_origins",
                f"contains an invalid origin: {origin!r}",
            )
        normalized.append(origin.rstrip("/"))
    return tuple(dict.fromkeys(normalized))


def _normalize_paths(value: Any, field_name: str) -> tuple[Path, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        _invalid_config(
            ErrorCode.INVALID_SYSTEM_CONFIG,
            field_name,
            "must be a list of filesystem paths",
        )
    normalized: list[Path] = []
    for path in value:
        if not isinstance(path, (str, Path)):
            _invalid_config(
                ErrorCode.INVALID_SYSTEM_CONFIG,
                field_name,
                "must contain only filesystem paths",
            )
        normalized.append(Path(path).expanduser().resolve())
    return tuple(dict.fromkeys(normalized))


def _normalize_hosts(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)) or not value:
        _invalid_config(
            ErrorCode.INVALID_SYSTEM_CONFIG,
            "allowed_hosts",
            "must be a non-empty list of hostnames without ports",
        )
    normalized: list[str] = []
    for host in value:
        valid_ipv6 = False
        if isinstance(host, str) and host.startswith("[") and host.endswith("]"):
            try:
                valid_ipv6 = isinstance(ip_address(host[1:-1]), IPv6Address)
            except ValueError:
                pass
        valid_name = (
            isinstance(host, str)
            and len(host) <= 253
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host) is not None
            and ".." not in host
        )
        if not (valid_ipv6 or valid_name):
            _invalid_config(
                ErrorCode.INVALID_SYSTEM_CONFIG,
                "allowed_hosts",
                "must contain only hostnames without ports",
            )
        normalized.append(host.lower())
    return tuple(dict.fromkeys(normalized))


def _require_schema_version(
    value: Any,
    expected: int,
    error_code: ErrorCode,
    label: str,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise RavenError(
            error_code,
            f"Unsupported {label} configuration schema version.",
            details={"schema_version": value, "supported_version": expected},
        )


def _require_nonempty_string(value: Any, field_name: str, error_code: ErrorCode) -> None:
    if not isinstance(value, str) or not value.strip():
        _invalid_config(error_code, field_name, "must be a non-empty string")


def _require_positive_number(value: Any, field_name: str, error_code: ErrorCode) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        _invalid_config(error_code, field_name, "must be a finite positive number")


def _require_nonnegative_number(value: Any, field_name: str, error_code: ErrorCode) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        _invalid_config(error_code, field_name, "must be a finite non-negative number")


def _require_positive_int(value: Any, field_name: str, error_code: ErrorCode) -> None:
    _require_int_range(value, field_name, error_code, minimum=1)


def _require_nonnegative_int(value: Any, field_name: str, error_code: ErrorCode) -> None:
    _require_int_range(value, field_name, error_code, minimum=0)


def _require_int_range(
    value: Any,
    field_name: str,
    error_code: ErrorCode,
    *,
    minimum: int,
    maximum: int | None = None,
) -> None:
    invalid = isinstance(value, bool) or not isinstance(value, int) or value < minimum
    if maximum is not None:
        invalid = invalid or value > maximum
    if invalid:
        range_text = (
            f"between {minimum} and {maximum}"
            if maximum is not None
            else f"at least {minimum}"
        )
        _invalid_config(error_code, field_name, f"must be an integer {range_text}")


def _invalid_config(error_code: ErrorCode, field_name: str, reason: str) -> None:
    raise RavenError(
        error_code,
        f"Invalid configuration field '{field_name}': {reason}.",
        details={"field": field_name, "reason": reason},
    )
