"""Private browser-upload sources retained only for bounded ingestion retries."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from .errors import ErrorCode, RavenError


MANIFEST_NAME = "manifest.json"
BROWSER_UPLOAD_DIR = "browser"


def create_upload_source(
    uploads_dir: Path,
    filename: str,
    retention_seconds: float,
) -> Path:
    """Reserve a unique user-private directory with a durable UTC expiry."""
    root = uploads_dir / BROWSER_UPLOAD_DIR
    directory = root / str(uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / filename
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=retention_seconds)
    try:
        _write_manifest(
            directory,
            {"expires_at": expires_at.isoformat(), "source_name": filename},
        )
    except BaseException:
        directory.rmdir()
        raise
    return source


def bind_upload_operation(source: Path, operation_id: UUID, task_id: UUID) -> None:
    manifest = _read_manifest(source.parent)
    manifest["operation_id"] = str(operation_id)
    manifest["task_id"] = str(task_id)
    _write_manifest(source.parent, manifest)


def check_upload_retry(source: Path, uploads_dir: Path) -> None:
    """Reject a managed source after expiry without consuming a retry attempt."""
    root = (uploads_dir / BROWSER_UPLOAD_DIR).resolve()
    candidate = Path(os.path.abspath(source))
    if not candidate.is_relative_to(root):
        return
    source = candidate.resolve()
    if candidate.parent.parent != root or source.parent != candidate.parent:
        raise RavenError(
            ErrorCode.INVALID_RETRY_INPUT,
            "The managed upload retry source is invalid.",
        )
    manifest = _read_manifest(source.parent)
    if manifest.get("source_name") != source.name:
        raise RavenError(
            ErrorCode.INVALID_RETRY_INPUT,
            "The managed upload retry source does not match its record.",
        )
    try:
        expiry = datetime.fromisoformat(str(manifest["expires_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RavenError(
            ErrorCode.INVALID_RETRY_INPUT,
            "The managed upload expiry record is invalid.",
        ) from exc
    if expiry.tzinfo is None or datetime.now(timezone.utc) >= expiry:
        raise RavenError(
            ErrorCode.UPLOAD_RETRY_EXPIRED,
            "The upload retry window has expired. Upload the file again.",
        )
    if not source.is_file():
        raise RavenError(
            ErrorCode.UPLOAD_RETRY_EXPIRED,
            "The temporary upload is no longer available. Upload it again.",
        )


def remove_upload_source(source: Path, uploads_dir: Path) -> None:
    """Remove only an exact managed source and its empty UUID directory."""
    root = (uploads_dir / BROWSER_UPLOAD_DIR).resolve()
    directory = source.parent.resolve()
    if directory.parent != root or source.resolve().parent != directory:
        raise RavenError(
            ErrorCode.INVALID_RETRY_INPUT,
            "The temporary upload cleanup target is invalid.",
        )
    source.unlink(missing_ok=True)
    (directory / MANIFEST_NAME).unlink(missing_ok=True)
    directory.rmdir()


def cleanup_expired_uploads(
    uploads_dir: Path,
    active_sources: frozenset[Path] = frozenset(),
) -> int:
    """Prune expired records, leaving every active operation source untouched."""
    root = uploads_dir / BROWSER_UPLOAD_DIR
    if not root.is_dir():
        return 0
    removed = 0
    current = datetime.now(timezone.utc)
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        try:
            UUID(directory.name)
            manifest = _read_manifest(directory)
            filename = manifest["source_name"]
            expiry = datetime.fromisoformat(manifest["expires_at"])
            source = directory / filename
            if expiry.tzinfo is None or expiry > current:
                continue
            if source.resolve() in active_sources:
                continue
            remove_upload_source(source, uploads_dir)
            removed += 1
        except (KeyError, OSError, TypeError, ValueError, RavenError):
            # A malformed record is left for manual inspection, not deleted.
            continue
    return removed


def _read_manifest(directory: Path) -> dict[str, str]:
    try:
        value = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RavenError(
            ErrorCode.INVALID_RETRY_INPUT,
            "The managed upload record is missing or invalid.",
        ) from exc
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise RavenError(
            ErrorCode.INVALID_RETRY_INPUT,
            "The managed upload record is invalid.",
        )
    return value


def _write_manifest(directory: Path, value: dict[str, str]) -> None:
    temporary = directory / f".{MANIFEST_NAME}.{uuid4()}.tmp"
    try:
        with temporary.open("w", encoding="utf-8") as output:
            output.write(json.dumps(value, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / MANIFEST_NAME)
    finally:
        temporary.unlink(missing_ok=True)
