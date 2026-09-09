"""Knowledge database entities and their registry for Raven."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID, uuid5

import qdrant_client
from llama_index.core.node_parser import SentenceSplitter
from qdrant_client.http import models as qmodels

from ..core.config import DEFAULT_OPEN_KNOWLEDGE_LIMIT, PathConfig
from ..core.errors import ErrorCode, RavenError, error_payload
from ..core.events import Event, EventType
from ..core.operations import Operation, OperationManager, OperationTask, OperationType
from ..providers.model_identity import embedding_identity

COLLECTION_NAME = "chunks"
PERSISTENCE_VERSION = 1
FILE_STORE_SCHEMA_VERSION = 1

KEY_FILE_ID = "file_id"
KEY_SECTION_ID = "section_id"
KEY_CHUNK_INDEX = "chunk_index"

POINT_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

logger = logging.getLogger(__name__)
_ResultT = TypeVar("_ResultT")


def _safe_name(name: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]", "_", name.strip())
    value = value.strip("_")
    if not value:
        raise RavenError(
            ErrorCode.INVALID_KNOWLEDGE_NAME,
            "Knowledge name must contain at least one valid character.",
        )
    return value


def _point_id(file_id: str, section_index: int, chunk_index: int) -> str:
    return str(uuid5(POINT_NAMESPACE, f"{file_id}:{section_index}:{chunk_index}"))




class _KnowledgeFileStore:
    """Persist one knowledge database's file and section catalog."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()


    def open(self) -> None:
        """Open the database and initialize its schema."""
        with self._lock:
            if self._connection is not None:
                return

            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                timeout=5.0,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row

            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA busy_timeout = 5000")

                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, FILE_STORE_SCHEMA_VERSION}:
                    raise RavenError(
                        ErrorCode.UNSUPPORTED_METADATA_VERSION,
                        f"Unsupported file database version for '{self.path.parent.name}'.",
                        details={"schema_version": version},
                    )

                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS files (
                        file_id TEXT PRIMARY KEY,
                        file_name TEXT NOT NULL UNIQUE,
                        section_count INTEGER NOT NULL,
                        chunk_count INTEGER NOT NULL,
                        ingested_at TEXT NOT NULL,
                        navigation_type TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS sections (
                        section_id TEXT PRIMARY KEY,
                        file_id TEXT NOT NULL,
                        section_index INTEGER NOT NULL,
                        summary TEXT NOT NULL,
                        keywords TEXT NOT NULL,
                        conditions TEXT NOT NULL,
                        definitions TEXT NOT NULL,
                        raw_content TEXT NOT NULL,
                        source_element_ids TEXT NOT NULL,
                        source_range TEXT,
                        FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE CASCADE,
                        UNIQUE (file_id, section_index)
                    );

                    CREATE INDEX IF NOT EXISTS idx_sections_file_id
                    ON sections(file_id, section_index);

                    CREATE TABLE IF NOT EXISTS pending_file_deletions (
                        file_id TEXT PRIMARY KEY,
                        file_name TEXT NOT NULL,
                        started_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    f"PRAGMA user_version = {FILE_STORE_SCHEMA_VERSION}"
                )
                connection.commit()
            except Exception:
                connection.close()
                raise

            self._connection = connection


    def close(self) -> None:
        """Checkpoint pending WAL pages and close the database."""
        with self._lock:
            connection = self._connection
            if connection is None:
                return

            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
            self._connection = None


    def file_exists(self, file_name: str) -> bool:
        with self._lock:
            row = self._require_connection().execute(
                "SELECT 1 FROM files WHERE file_name = ? LIMIT 1",
                (file_name,),
            ).fetchone()
        return row is not None


    def list_files(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._require_connection().execute(
                """
                SELECT file_id, file_name, section_count, chunk_count,
                       ingested_at, navigation_type
                FROM files
                ORDER BY rowid
                """
            ).fetchall()
        return [self._file_from_row(row) for row in rows]


    def get_file(self, file_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._require_connection().execute(
                """
                SELECT file_id, file_name, section_count, chunk_count,
                       ingested_at, navigation_type
                FROM files
                WHERE file_id = ?
                """,
                (file_id,),
            ).fetchone()
        return None if row is None else self._file_from_row(row)


    def list_sections(self, file_name: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._require_connection().execute(
                """
                SELECT s.*, f.file_name
                FROM sections AS s
                JOIN files AS f ON f.file_id = s.file_id
                WHERE f.file_name = ?
                ORDER BY s.section_index
                """,
                (file_name,),
            ).fetchall()
        return [self._section_from_row(row) for row in rows]


    def get_section(self, section_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._require_connection().execute(
                """
                SELECT s.*, f.file_name
                FROM sections AS s
                JOIN files AS f ON f.file_id = s.file_id
                WHERE s.section_id = ?
                """,
                (section_id,),
            ).fetchone()
        return None if row is None else self._section_from_row(row)


    def add_file(
        self,
        file: dict[str, Any],
        sections: Sequence[dict[str, Any]],
    ) -> None:
        """Atomically insert one file and all of its sections."""
        with self._lock:
            connection = self._require_connection()
            try:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO files (
                            file_id, file_name, section_count, chunk_count,
                            ingested_at, navigation_type
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            file["file_id"],
                            file["file_name"],
                            file["section_count"],
                            file["chunk_count"],
                            file["ingested_at"],
                            file["navigation_type"],
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO sections (
                            section_id, file_id, section_index, summary,
                            keywords, conditions, definitions, raw_content,
                            source_element_ids, source_range
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [self._section_values(section) for section in sections],
                    )
            except sqlite3.IntegrityError as exc:
                raise RavenError(
                    ErrorCode.FILE_ALREADY_EXISTS,
                    f"File '{file['file_name']}' already exists.",
                ) from exc


    def remove_file(self, file_id: str) -> dict[str, Any] | None:
        """Atomically remove a file and return data that can restore it."""
        with self._lock:
            connection = self._require_connection()
            file_row = connection.execute(
                "SELECT * FROM files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if file_row is None:
                return None

            section_rows = connection.execute(
                "SELECT * FROM sections WHERE file_id = ? ORDER BY section_index",
                (file_id,),
            ).fetchall()
            bundle = {
                "file": dict(file_row),
                "sections": [dict(row) for row in section_rows],
            }
            with connection:
                connection.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
            return bundle


    def begin_file_deletion(self, file_id: str) -> dict[str, str] | None:
        """Durably mark a file for deletion before touching its vectors."""
        with self._lock:
            connection = self._require_connection()
            pending = connection.execute(
                "SELECT * FROM pending_file_deletions WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if pending is not None:
                return {
                    "file_id": str(pending["file_id"]),
                    "file_name": str(pending["file_name"]),
                    "started_at": str(pending["started_at"]),
                }

            file_row = connection.execute(
                "SELECT file_id, file_name FROM files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if file_row is None:
                return None

            record = {
                "file_id": str(file_row["file_id"]),
                "file_name": str(file_row["file_name"]),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            with connection:
                connection.execute(
                    """
                    INSERT INTO pending_file_deletions (
                        file_id, file_name, started_at
                    ) VALUES (?, ?, ?)
                    """,
                    (record["file_id"], record["file_name"], record["started_at"]),
                )
            return record


    def finish_file_deletion(self, file_id: str) -> None:
        """Atomically remove catalog data and its durable deletion marker."""
        with self._lock:
            connection = self._require_connection()
            with connection:
                connection.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
                connection.execute(
                    "DELETE FROM pending_file_deletions WHERE file_id = ?",
                    (file_id,),
                )


    def list_pending_file_deletions(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._require_connection().execute(
                "SELECT * FROM pending_file_deletions ORDER BY started_at"
            ).fetchall()
        return [
            {
                "file_id": str(row["file_id"]),
                "file_name": str(row["file_name"]),
                "started_at": str(row["started_at"]),
            }
            for row in rows
        ]


    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._require_connection().execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
        return None if row is None else str(row["value"])


    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            connection = self._require_connection()
            with connection:
                connection.execute(
                    """
                    INSERT INTO settings (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )


    def restore_file(self, bundle: dict[str, Any]) -> None:
        """Restore a bundle returned by :meth:`remove_file`."""
        file = bundle["file"]
        sections = bundle["sections"]
        with self._lock:
            connection = self._require_connection()
            with connection:
                connection.execute(
                    """
                    INSERT INTO files (
                        file_id, file_name, section_count, chunk_count,
                        ingested_at, navigation_type
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file["file_id"],
                        file["file_name"],
                        file["section_count"],
                        file["chunk_count"],
                        file["ingested_at"],
                        file["navigation_type"],
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO sections (
                        section_id, file_id, section_index, summary,
                        keywords, conditions, definitions, raw_content,
                        source_element_ids, source_range
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            section["section_id"],
                            section["file_id"],
                            section["section_index"],
                            section["summary"],
                            section["keywords"],
                            section["conditions"],
                            section["definitions"],
                            section["raw_content"],
                            section["source_element_ids"],
                            section["source_range"],
                        )
                        for section in sections
                    ],
                )


    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RavenError(
                ErrorCode.KNOWLEDGE_NOT_STARTED,
                f"File database '{self.path}' is not open.",
            )
        return self._connection


    @staticmethod
    def _file_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "file_id": str(row["file_id"]),
            "file_name": str(row["file_name"]),
            "section_count": int(row["section_count"]),
            "chunk_count": int(row["chunk_count"]),
            "ingested_at": str(row["ingested_at"]),
            "navigation_type": str(row["navigation_type"]),
        }


    @staticmethod
    def _section_values(section: dict[str, Any]) -> tuple[Any, ...]:
        source_range = section.get("source_range")
        return (
            section["section_id"],
            section["file_id"],
            section["section_index"],
            str(section.get("summary", "")),
            json.dumps(section.get("keywords", []), ensure_ascii=False),
            json.dumps(section.get("conditions", []), ensure_ascii=False),
            json.dumps(section.get("definitions", []), ensure_ascii=False),
            str(section.get("raw_content", "")),
            json.dumps(section.get("source_element_ids", []), ensure_ascii=False),
            None if source_range is None else json.dumps(source_range),
        )


    @staticmethod
    def _section_from_row(row: sqlite3.Row) -> dict[str, Any]:
        source_range = row["source_range"]
        return {
            "file_id": str(row["file_id"]),
            "file_name": str(row["file_name"]),
            "section_index": int(row["section_index"]),
            "summary": str(row["summary"]),
            "keywords": json.loads(row["keywords"]),
            "conditions": json.loads(row["conditions"]),
            "definitions": json.loads(row["definitions"]),
            "raw_content": str(row["raw_content"]),
            "source_element_ids": json.loads(row["source_element_ids"]),
            "source_range": None if source_range is None else json.loads(source_range),
            "section_id": str(row["section_id"]),
        }




class Knowledge:
    """One self-contained knowledge database."""

    def __init__(
        self,
        dir_path: Path,
        *,
        operation_manager: OperationManager,
        before_open: Callable[["Knowledge"], Awaitable[None]] | None = None,
        after_open: Callable[["Knowledge"], Awaitable[None]] | None = None,
    ) -> None:
        self.dir_path = Path(dir_path)
        self.name = self.dir_path.name
        self.qdrant_dir = self.dir_path / "qdrant"
        self.meta_path = self.dir_path / "meta.json"
        self.files_path = self.dir_path / "file.sqlite3"

        self._qdrant: qdrant_client.QdrantClient | None = None
        self._file_store = _KnowledgeFileStore(self.files_path)
        self._meta: dict[str, Any] = {}
        self._start_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self._pending_ingestions: set[str] = set()
        self._ingestion_claims: dict[str, str] = {}
        self._started = False
        self._closed = False
        self._operation_manager = operation_manager
        self._before_open = before_open
        self._after_open = after_open
        self._usage_lock = threading.Lock()
        self._active_uses = 0


    @property
    def safe_name(self) -> str:
        return self.name


    @property
    def is_started(self) -> bool:
        return self._started and not self._closed


    @property
    def active_uses(self) -> int:
        with self._usage_lock:
            return self._active_uses


    @property
    def meta(self) -> dict[str, Any]:
        if self._closed:
            raise RavenError(
                ErrorCode.KNOWLEDGE_CLOSED,
                f"Knowledge '{self.name}' is closed.",
            )
        return copy.deepcopy(self._meta)


    async def start(self) -> None:
        """Open this knowledge database and load its metadata."""
        async with self._start_lock:
            if self._started and not self._closed:
                return
            if self._closed:
                raise RavenError(
                    ErrorCode.KNOWLEDGE_CLOSED,
                    f"Knowledge '{self.name}' is closed.",
                )

            reserved = False
            try:
                if self._before_open is not None:
                    await self._before_open(self)
                    reserved = True

                async with self._lifecycle_lock:
                    if self._started and not self._closed:
                        return
                    if self._closed:
                        raise RavenError(
                            ErrorCode.KNOWLEDGE_CLOSED,
                            f"Knowledge '{self.name}' is closed.",
                        )
                    await asyncio.to_thread(self._open_storage)
                    self._started = True
                    await self._reconcile_pending_file_deletions()
            finally:
                if reserved and self._after_open is not None:
                    await self._after_open(self)


    async def close(self) -> None:
        """Close the local Qdrant client."""
        cleanup_error: BaseException | None = None
        if self.is_started:
            try:
                await self.reconcile_pending_ingestions()
                await self.reconcile_pending_file_deletions()
            except BaseException as exc:
                cleanup_error = exc

        async with self._lifecycle_lock:
            if self._closed:
                return

            qdrant = self._qdrant
            self._qdrant = None
            self._closed = True
            self._started = False
            await asyncio.to_thread(self._file_store.close)
            if qdrant is not None:
                await asyncio.to_thread(qdrant.close)

        if cleanup_error is not None:
            raise cleanup_error


    async def release_resources(self) -> bool:
        """Close idle storage while keeping this knowledge reopenable."""
        async with self._lifecycle_lock:
            if not self.is_started:
                return True
            with self._usage_lock:
                if self._active_uses:
                    return False
                qdrant = self._qdrant
                self._qdrant = None
                self._started = False
            await asyncio.to_thread(self._file_store.close)
            if qdrant is not None:
                await asyncio.to_thread(qdrant.close)
            return True


    async def _run_in_use(
        self,
        worker: Callable[[], Awaitable[_ResultT]],
    ) -> _ResultT:
        async with self._lifecycle_lock:
            if self._closed:
                raise RavenError(
                    ErrorCode.KNOWLEDGE_CLOSED,
                    f"Knowledge '{self.name}' is closed.",
                )
            with self._usage_lock:
                self._active_uses += 1
        try:
            await self.start()
            return await worker()
        finally:
            with self._usage_lock:
                self._active_uses -= 1


    async def claim_ingestion(self, file_name: str, file_id: str) -> None:
        """Prevent concurrent pipelines from parsing the same target file."""
        async with self._mutation_lock:
            if await asyncio.to_thread(self._file_store.file_exists, file_name):
                existing = await asyncio.to_thread(self._file_result_by_id, file_id)
                if existing is not None and existing["file"] == file_name:
                    return
                raise RavenError(
                    ErrorCode.FILE_ALREADY_EXISTS,
                    f"File '{file_name}' already exists in knowledge '{self.name}'.",
                )
            owner = self._ingestion_claims.get(file_name)
            if owner is not None and owner != file_id:
                raise RavenError(
                    ErrorCode.FILE_ALREADY_EXISTS,
                    f"File '{file_name}' is already being ingested.",
                )
            self._ingestion_claims[file_name] = file_id


    async def release_ingestion(self, file_name: str, file_id: str) -> None:
        async with self._mutation_lock:
            if self._ingestion_claims.get(file_name) == file_id:
                self._ingestion_claims.pop(file_name, None)


    async def validate_embedding_model(self, embed_model: Any) -> None:
        """Reject adapters that do not match this knowledge's vector space."""
        async def validate() -> None:
            async with self._mutation_lock:
                await asyncio.to_thread(self._ensure_embedding_identity, embed_model)

        await self._run_in_use(validate)


    async def set_summary(
        self,
        summary: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.KNOWLEDGE_SET_SUMMARY
        )
        return await active_operation.run(
            OperationType.KNOWLEDGE_SET_SUMMARY,
            lambda active_operation: self._run_in_use(
                lambda: self._set_summary(
                    summary,
                    operation=active_operation,
                )
            ),
        )


    async def _set_summary(
        self,
        summary: str,
        *,
        operation: Operation,
    ) -> None:
        """Persist the user-facing summary for this knowledge database."""
        self._ensure_started()
        async with self._mutation_lock:
            meta = copy.deepcopy(self._meta)
            meta["user_summary"] = summary
            await asyncio.to_thread(self._write_json, self.meta_path, meta)
            self._meta = meta

        await self._emit(
            operation,
            EventType.KNOWLEDGE_UPDATED,
            {"knowledge": self.name, "user_summary": summary},
        )


    def get_summary(self) -> str:
        return str(self._meta.get("user_summary", ""))


    def file_exists(self, file_name: str) -> bool:
        return self._read_file_store("file_exists", file_name)


    def list_files(self) -> list[dict[str, Any]]:
        return self._read_file_store("list_files")


    def list_sections(self, file_name: str) -> list[dict[str, Any]]:
        return self._read_file_store("list_sections", file_name)


    def get_section(self, section_id: str) -> dict[str, Any] | None:
        return self._read_file_store("get_section", section_id)


    async def ingest(
        self,
        file_name: str,
        sections: Sequence[dict[str, Any]],
        *,
        file_id: str,
        embed_model: Any,
        navigation_type: str = "none",
        chunk_size: int = 512,
        chunk_overlap: int = 50,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.KNOWLEDGE_INGEST
        )
        return await active_operation.run(
            OperationType.KNOWLEDGE_INGEST,
            lambda active_operation: self._run_in_use(
                lambda: self._ingest(
                    file_name,
                    sections,
                    file_id=file_id,
                    embed_model=embed_model,
                    navigation_type=navigation_type,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    operation=active_operation,
                )
            ),
        )


    async def reconcile_ingestion(
        self,
        file_id: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Remove uncommitted vectors or report an already committed ingestion."""
        active_operation = operation or await self._operation_manager.create(
            OperationType.INGESTION_CLEANUP
        )
        return await active_operation.run(
            OperationType.INGESTION_CLEANUP,
            lambda active_operation: self._run_in_use(
                lambda: self._reconcile_ingestion(
                    file_id,
                    operation=active_operation,
                )
            ),
        )


    async def _reconcile_ingestion(
        self,
        file_id: str,
        *,
        operation: Operation,
    ) -> dict[str, Any]:
        self._ensure_started()
        await self._emit(
            operation,
            EventType.INGESTION_CLEANUP_STARTED,
            {"knowledge": self.name, "file_id": file_id},
        )

        try:
            async with self._mutation_lock:
                committed = await asyncio.to_thread(
                    self._file_result_by_id,
                    file_id,
                )
                if committed is not None:
                    result = {
                        "knowledge": self.name,
                        "file_id": file_id,
                        "status": "committed",
                        "points_removed": 0,
                        "ingestion": committed,
                    }
                else:
                    points_removed = await asyncio.to_thread(
                        self._count_points_by_file_id,
                        file_id,
                    )
                    await asyncio.to_thread(self._delete_points_by_file_id, file_id)
                    self._pending_ingestions.discard(file_id)
                    result = {
                        "knowledge": self.name,
                        "file_id": file_id,
                        "status": "cleaned",
                        "points_removed": points_removed,
                    }
        except Exception as exc:
            await self._emit(
                operation,
                EventType.INGESTION_CLEANUP_FAILED,
                {
                    "knowledge": self.name,
                    "file_id": file_id,
                    "error": error_payload(exc),
                },
            )
            raise RavenError(
                ErrorCode.INGESTION_RECONCILIATION_FAILED,
                f"Could not reconcile ingestion '{file_id}' in knowledge '{self.name}'.",
                details={"knowledge": self.name, "file_id": file_id},
            ) from exc

        await self._emit(operation, EventType.INGESTION_CLEANUP_COMPLETED, result)
        return result


    async def reconcile_pending_ingestions(self) -> list[dict[str, Any]]:
        """Perform the final targeted cleanup used during graceful shutdown."""
        async def reconcile() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            async with self._mutation_lock:
                for file_id in tuple(self._pending_ingestions):
                    committed = await asyncio.to_thread(
                        self._file_result_by_id,
                        file_id,
                    )
                    if committed is not None:
                        self._pending_ingestions.discard(file_id)
                        results.append(
                            {
                                "knowledge": self.name,
                                "file_id": file_id,
                                "status": "committed",
                                "points_removed": 0,
                            }
                        )
                        continue

                    points_removed = await asyncio.to_thread(
                        self._count_points_by_file_id,
                        file_id,
                    )
                    await asyncio.to_thread(self._delete_points_by_file_id, file_id)
                    self._pending_ingestions.discard(file_id)
                    results.append(
                        {
                            "knowledge": self.name,
                            "file_id": file_id,
                            "status": "cleaned",
                            "points_removed": points_removed,
                        }
                    )
            return results

        return await self._run_in_use(reconcile)


    async def reconcile_pending_file_deletions(self) -> list[dict[str, Any]]:
        """Finish file deletions that were interrupted after their durable marker."""
        return await self._run_in_use(self._reconcile_pending_file_deletions)


    async def _reconcile_pending_file_deletions(self) -> list[dict[str, Any]]:
        self._ensure_started()
        results: list[dict[str, Any]] = []
        async with self._mutation_lock:
            pending = await asyncio.to_thread(
                self._file_store.list_pending_file_deletions
            )
            for record in pending:
                file_id = record["file_id"]
                await asyncio.to_thread(self._delete_points_by_file_id, file_id)
                finish_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._file_store.finish_file_deletion,
                        file_id,
                    )
                )
                try:
                    await asyncio.shield(finish_task)
                except asyncio.CancelledError:
                    await finish_task
                    raise
                results.append(
                    {
                        "knowledge": self.name,
                        "file_id": file_id,
                        "file": record["file_name"],
                        "status": "deleted",
                    }
                )
        return results


    async def _ingest(
        self,
        file_name: str,
        sections: Sequence[dict[str, Any]],
        *,
        file_id: str,
        embed_model: Any,
        navigation_type: str = "none",
        chunk_size: int = 512,
        chunk_overlap: int = 50,
        operation: Operation,
    ) -> dict[str, Any]:
        """Embed, index, and persist one file and its sections."""
        self._ensure_started()

        if not isinstance(file_id, str) or not file_id.strip():
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                "file_id must be a non-empty string.",
            )
        if embed_model is None:
            raise RavenError(
                ErrorCode.EMBEDDING_MODEL_REQUIRED,
                "An embedding model is required for ingestion.",
            )
        if chunk_size <= 0:
            raise RavenError(ErrorCode.INVALID_CHUNKING, "chunk_size must be positive.")
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise RavenError(
                ErrorCode.INVALID_CHUNKING,
                "chunk_overlap must be non-negative and smaller than chunk_size.",
            )

        sections = [dict(section) for section in sections]
        await self._emit(
            operation,
            EventType.KNOWLEDGE_INGEST_STARTED,
            {"knowledge": self.name, "file": file_name, "section_count": len(sections)},
        )

        async with self._mutation_lock:
            metadata_written = False
            cleanup_completed = False

            try:
                committed = await asyncio.to_thread(
                    self._file_result_by_id,
                    file_id,
                )
                if committed is not None:
                    if committed["file"] != file_name:
                        raise RavenError(
                            ErrorCode.INVALID_METADATA,
                            f"File ID '{file_id}' is already assigned to "
                            f"'{committed['file']}'.",
                        )
                    result = committed
                    metadata_written = True
                    cleanup_completed = True
                    await self._emit(
                        operation,
                        EventType.KNOWLEDGE_INGEST_COMPLETED,
                        result,
                    )
                    return result

                if await asyncio.to_thread(self._file_store.file_exists, file_name):
                    raise RavenError(
                        ErrorCode.FILE_ALREADY_EXISTS,
                        f"File '{file_name}' already exists in knowledge '{self.name}'.",
                    )

                chunk_records: list[tuple[str, str, int, int, dict[str, Any]]] = []

                for section_index, section in enumerate(sections, start=1):
                    texts = self._split(
                        section.get("raw_content", ""),
                        chunk_size,
                        chunk_overlap,
                    )
                    
                    for chunk_index, text in enumerate(texts):
                        chunk_records.append(
                            (
                                _point_id(file_id, section_index, chunk_index),
                                text,
                                section_index,
                                chunk_index,
                                section,
                            )
                        )

                if not chunk_records:
                    raise RavenError(
                        ErrorCode.NO_CHUNKS_PRODUCED,
                        f"No chunks were produced for file '{file_name}'.",
                    )

                if operation is not None:
                    operation.raise_if_cancelled()

                await asyncio.to_thread(
                    self._ensure_embedding_identity,
                    embed_model,
                )

                vectors = await embed_model.aget_text_embedding_batch(
                    [record[1] for record in chunk_records]
                )
                if len(vectors) != len(chunk_records):
                    raise RavenError(
                        ErrorCode.INVALID_EMBEDDING_RESULT,
                        "Embedding model returned an invalid number of vectors.",
                    )

                await asyncio.to_thread(self._ensure_collection, len(vectors[0]))
                points = [
                    qmodels.PointStruct(
                        id=point_id,
                        vector=list(vector),
                        payload={
                            KEY_FILE_ID: file_id,
                            KEY_SECTION_ID: f"{file_id}-{section_index}",
                            KEY_CHUNK_INDEX: chunk_index,
                        },
                    )
                    for (point_id, _text, section_index, chunk_index, _section), vector in zip(
                        chunk_records,
                        vectors,
                    )
                ]

                if operation is not None:
                    operation.raise_if_cancelled()
                self._pending_ingestions.add(file_id)
                upsert_task = asyncio.create_task(
                    asyncio.to_thread(self._upsert_points, points)
                )
                try:
                    await asyncio.shield(upsert_task)
                except asyncio.CancelledError:
                    await upsert_task
                    raise
                await self._emit(
                    operation,
                    EventType.INGESTION_VECTORS_WRITTEN,
                    {
                        "knowledge": self.name,
                        "file": file_name,
                        "file_id": file_id,
                        "point_count": len(points),
                    },
                )

                file_record = {
                    "file_id": file_id,
                    "file_name": file_name,
                    "section_count": len(sections),
                    "chunk_count": len(points),
                    "ingested_at": datetime.now(timezone.utc).isoformat(),
                    "navigation_type": navigation_type,
                }
                section_records: list[dict[str, Any]] = []
                for section_index, section in enumerate(sections, start=1):
                    section_id = f"{file_id}-{section_index}"
                    section_records.append({
                        "section_id": section_id,
                        "file_id": file_id,
                        "file_name": file_name,
                        "section_index": section_index,
                        "summary": section.get("summary", ""),
                        "keywords": section.get("keywords", []),
                        "conditions": section.get("conditions", []),
                        "definitions": section.get("definitions", []),
                        "raw_content": section.get("raw_content", ""),
                        "source_element_ids": section.get("source_element_ids", []),
                        "source_range": section.get("source_range"),
                    })

                metadata_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._file_store.add_file,
                        file_record,
                        section_records,
                    )
                )
                try:
                    await asyncio.shield(metadata_task)
                except asyncio.CancelledError:
                    await metadata_task
                    metadata_written = True
                    raise
                metadata_written = True
                self._pending_ingestions.discard(file_id)
                cleanup_completed = True
                await self._emit(
                    operation,
                    EventType.INGESTION_METADATA_COMMITTED,
                    {
                        "knowledge": self.name,
                        "file": file_name,
                        "file_id": file_id,
                    },
                )
                result = {
                    "knowledge": self.name,
                    "file": file_name,
                    "file_id": file_id,
                    "section_count": len(sections),
                    "chunk_count": len(points),
                }
            except asyncio.CancelledError:
                if not metadata_written:
                    await asyncio.to_thread(self._delete_points_by_file_id, file_id)
                    cleanup_completed = True
                raise
            except Exception as exc:
                if not metadata_written:
                    await asyncio.to_thread(self._delete_points_by_file_id, file_id)
                    cleanup_completed = True
                await self._emit(
                    operation,
                    EventType.KNOWLEDGE_INGEST_FAILED,
                    {
                        "knowledge": self.name,
                        "file": file_name,
                        "error": error_payload(exc),
                    },
                )
                raise
            finally:
                if metadata_written or cleanup_completed:
                    self._pending_ingestions.discard(file_id)

        await self._emit(operation, EventType.KNOWLEDGE_INGEST_COMPLETED, result)
        return result


    async def search(self, query_vector: list[float], top_k: int = 5) -> list[dict[str, Any]]:
        """Return vector hits containing only identifiers and scores."""
        if top_k <= 0:
            return []

        async def search_vectors() -> list[dict[str, Any]]:
            async with self._mutation_lock:
                return await asyncio.to_thread(self._search, query_vector, top_k)

        return await self._run_in_use(search_vectors)


    async def retrieve_context(
        self,
        query_vector: list[float],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Return complete sections reconstructed from vector hits."""
        hits = await self.search(query_vector, top_k=top_k)
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for hit in hits:
            section_id = hit.get(KEY_SECTION_ID)
            if not isinstance(section_id, str) or section_id in seen:
                continue
            section = await asyncio.to_thread(self.get_section, section_id)
            if section is None:
                continue
            seen.add(section_id)
            result.append(
                {
                    "knowledge_name": self.name,
                    "file_name": section.get("file_name"),
                    "section_id": section_id,
                    "raw_content": section.get("raw_content", ""),
                    "score": hit.get("score"),
                }
            )
        return result


    async def count(self) -> int:
        """Return the number of indexed vector points."""
        async def count_vectors() -> int:
            async with self._mutation_lock:
                return await asyncio.to_thread(self._count)

        return await self._run_in_use(count_vectors)


    async def delete_file(
        self,
        file_id: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.KNOWLEDGE_DELETE_FILE
        )
        return await active_operation.run(
            OperationType.KNOWLEDGE_DELETE_FILE,
            lambda active_operation: self._run_in_use(
                lambda: self._delete_file(
                    file_id,
                    operation=active_operation,
                )
            ),
        )


    async def _delete_file(
        self,
        file_id: str,
        *,
        operation: Operation,
    ) -> dict[str, Any]:
        """Remove a file's metadata and vectors."""
        self._ensure_started()
        await self._emit(
            operation,
            EventType.KNOWLEDGE_FILE_DELETE_STARTED,
            {"knowledge": self.name, "file_id": file_id},
        )

        async with self._mutation_lock:
            marker_task = asyncio.create_task(
                asyncio.to_thread(self._file_store.begin_file_deletion, file_id)
            )
            try:
                marker = await asyncio.shield(marker_task)
            except asyncio.CancelledError:
                await marker_task
                raise
            except Exception as exc:
                await self._emit(
                    operation,
                    EventType.KNOWLEDGE_FILE_DELETE_FAILED,
                    {
                        "knowledge": self.name,
                        "file_id": file_id,
                        "error": error_payload(exc),
                    },
                )
                raise
            if marker is None:
                error = RavenError(
                    ErrorCode.FILE_NOT_FOUND,
                    f"File '{file_id}' does not exist in knowledge '{self.name}'.",
                )
                await self._emit(
                    operation,
                    EventType.KNOWLEDGE_FILE_DELETE_FAILED,
                    {
                        "knowledge": self.name,
                        "file_id": file_id,
                        "error": error_payload(error),
                    },
                )
                raise error
            file_name = marker["file_name"]

            try:
                delete_task = asyncio.create_task(
                    asyncio.to_thread(self._delete_points_by_file_id, file_id)
                )
                try:
                    await asyncio.shield(delete_task)
                except asyncio.CancelledError:
                    await delete_task
                    raise
                finish_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._file_store.finish_file_deletion,
                        file_id,
                    )
                )
                try:
                    await asyncio.shield(finish_task)
                except asyncio.CancelledError:
                    await finish_task
                    raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._emit(
                    operation,
                    EventType.KNOWLEDGE_FILE_DELETE_FAILED,
                    {
                        "knowledge": self.name,
                        "file_id": file_id,
                        "error": error_payload(exc),
                    },
                )
                raise

            result = {"knowledge": self.name, "file_id": file_id, "file": file_name}

        await self._emit(operation, EventType.KNOWLEDGE_FILE_DELETE_COMPLETED, result)
        return result


    async def _emit(
        self,
        operation: Operation | None,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> None:
        if operation is not None:
            await operation.publish(Event(type=event_type, data=data or {}))


    def _read_file_store(self, method_name: str, *args: Any) -> Any:
        if self._closed:
            raise RavenError(
                ErrorCode.KNOWLEDGE_CLOSED,
                f"Knowledge '{self.name}' is closed.",
            )
        with self._usage_lock:
            self._active_uses += 1
        temporary_store: _KnowledgeFileStore | None = None
        try:
            store = self._file_store
            if not self.is_started:
                temporary_store = _KnowledgeFileStore(self.files_path)
                temporary_store.open()
                store = temporary_store
            return getattr(store, method_name)(*args)
        finally:
            if temporary_store is not None:
                temporary_store.close()
            with self._usage_lock:
                self._active_uses -= 1


    def _load_descriptor(self) -> None:
        self._meta = self._load_meta()


    def _open_storage(self) -> None:
        self.dir_path.mkdir(parents=True, exist_ok=True)
        self.qdrant_dir.mkdir(parents=True, exist_ok=True)
        self._meta = self._load_meta()
        self._file_store.open()
        try:
            self._qdrant = qdrant_client.QdrantClient(path=str(self.qdrant_dir))
        except Exception:
            self._file_store.close()
            raise


    def _load_meta(self) -> dict[str, Any]:
        if not self.meta_path.exists():
            meta = {
                "schema_version": PERSISTENCE_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "name": self.name,
                "user_summary": "",
            }
            self._write_json(self.meta_path, meta)
            return meta

        meta = self._read_json(self.meta_path)
        if not isinstance(meta, dict):
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                f"Invalid metadata for knowledge '{self.name}'.",
            )
        if meta.get("schema_version", PERSISTENCE_VERSION) != PERSISTENCE_VERSION:
            raise RavenError(
                ErrorCode.UNSUPPORTED_METADATA_VERSION,
                f"Unsupported metadata version for knowledge '{self.name}'.",
            )

        normalized = {
            "schema_version": PERSISTENCE_VERSION,
            "created_at": meta.get("created_at") or datetime.now(timezone.utc).isoformat(),
            "name": self.name,
            "user_summary": meta.get("user_summary", ""),
        }
        if normalized != meta:
            self._write_json(self.meta_path, normalized)
        return normalized


    def _split(self, text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
        splitter = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        return splitter.split_text_metadata_aware(text, "")


    def _file_result_by_id(self, file_id: str) -> dict[str, Any] | None:
        file = self._file_store.get_file(file_id)
        if file is None:
            return None
        return {
            "knowledge": self.name,
            "file": file["file_name"],
            "file_id": file["file_id"],
            "section_count": file["section_count"],
            "chunk_count": file["chunk_count"],
        }


    def _collection_exists(self) -> bool:
        return bool(self._require_qdrant().collection_exists(COLLECTION_NAME))


    def _ensure_collection(self, dimension: int) -> None:
        qdrant = self._require_qdrant()
        if not qdrant.collection_exists(COLLECTION_NAME):
            qdrant.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=qmodels.VectorParams(
                    size=dimension,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            return

        existing = qdrant.get_collection(COLLECTION_NAME)

        vectors = existing.config.params.vectors
        if isinstance(vectors, dict):
            sizes = [vector.size for vector in vectors.values() if vector is not None]
            current_dimension = sizes[0] if sizes else None
        elif isinstance(vectors, qmodels.VectorParams):
            current_dimension = vectors.size
        else:
            current_dimension = None

        if current_dimension != dimension:
            raise RavenError(
                ErrorCode.EMBEDDING_DIMENSION_MISMATCH,
                f"Embedding dimension mismatch for knowledge '{self.name}'.",
                details={
                    "collection_dimension": current_dimension,
                    "model_dimension": dimension,
                },
            )


    def _ensure_embedding_identity(self, embed_model: Any) -> None:
        current = embedding_identity(embed_model)
        stored = self._file_store.get_setting("embedding_identity")
        if stored is None:
            self._file_store.set_setting("embedding_identity", current)
            return
        if stored != current:
            raise RavenError(
                ErrorCode.EMBEDDING_IDENTITY_MISMATCH,
                f"Embedding model does not match knowledge '{self.name}'.",
                details={"knowledge": self.name},
            )


    def _upsert_points(self, points: list[qmodels.PointStruct]) -> None:
        if points:
            self._require_qdrant().upsert(
                collection_name=COLLECTION_NAME,
                points=points,
                wait=True,
            )


    def _delete_points_by_file_id(self, file_id: str) -> None:
        if not self._collection_exists():
            return
        self._require_qdrant().delete(
            collection_name=COLLECTION_NAME,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key=KEY_FILE_ID,
                            match=qmodels.MatchValue(value=file_id),
                        )
                    ]
                )
            ),
            wait=True,
        )


    def _count_points_by_file_id(self, file_id: str) -> int:
        if not self._collection_exists():
            return 0
        return self._require_qdrant().count(
            collection_name=COLLECTION_NAME,
            count_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key=KEY_FILE_ID,
                        match=qmodels.MatchValue(value=file_id),
                    )
                ]
            ),
            exact=True,
        ).count


    def _search(self, query_vector: list[float], top_k: int) -> list[dict[str, Any]]:
        if not self._collection_exists():
            return []
        response = self._require_qdrant().query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            limit=top_k,
            with_payload=True,
        )
        return [
            {
                KEY_FILE_ID: (point.payload or {}).get(KEY_FILE_ID),
                KEY_SECTION_ID: (point.payload or {}).get(KEY_SECTION_ID),
                KEY_CHUNK_INDEX: (point.payload or {}).get(KEY_CHUNK_INDEX),
                "score": point.score,
            }
            for point in response.points
        ]


    def _count(self) -> int:
        if not self._collection_exists():
            return 0
        return self._require_qdrant().count(
            collection_name=COLLECTION_NAME,
            exact=True,
        ).count


    def _require_qdrant(self) -> qdrant_client.QdrantClient:
        if self._qdrant is None:
            raise RavenError(
                ErrorCode.KNOWLEDGE_NOT_STARTED,
                f"Knowledge '{self.name}' is not started.",
            )
        return self._qdrant


    def _ensure_started(self) -> None:
        if not self.is_started:
            raise RavenError(
                ErrorCode.KNOWLEDGE_NOT_STARTED,
                f"Knowledge '{self.name}' is not started.",
            )


    @staticmethod
    def _read_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))


    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temporary, path)




class KnowledgeBase:
    """Registry and lifecycle manager for separate knowledge databases."""

    def __init__(
        self,
        paths: PathConfig,
        operation_manager: OperationManager,
        *,
        max_open_knowledges: int = DEFAULT_OPEN_KNOWLEDGE_LIMIT,
    ) -> None:
        if (
            isinstance(max_open_knowledges, bool)
            or not isinstance(max_open_knowledges, int)
            or max_open_knowledges <= 0
        ):
            raise RavenError(
                ErrorCode.INVALID_RESOURCE_CACHE_SIZE,
                "Open knowledge limit must be a positive integer.",
            )
        self.paths = paths
        self.knowledge_base_path = paths.knowledge_base_dir
        self.max_open_knowledges = max_open_knowledges
        self._knowledges: OrderedDict[str, Knowledge] = OrderedDict()
        self._lifecycle_lock = asyncio.Lock()
        self._resource_lock = asyncio.Lock()
        self._opening_knowledges: set[int] = set()
        self._started = False
        self._closed = False
        self._operation_manager = operation_manager


    @property
    def is_started(self) -> bool:
        return self._started and not self._closed


    async def start(self) -> None:
        """Discover and open existing knowledge databases."""
        async with self._lifecycle_lock:
            if self._started and not self._closed:
                return
            if self._closed:
                raise RavenError(
                    ErrorCode.KNOWLEDGE_CLOSED,
                    "Knowledge base is closed.",
                )

            await asyncio.to_thread(self.knowledge_base_path.mkdir, parents=True, exist_ok=True)
            names = await asyncio.to_thread(self._scan_existing)
            for name in names:
                knowledge = Knowledge(
                    self.knowledge_base_path / name,
                    operation_manager=self._operation_manager,
                    before_open=self._before_knowledge_open,
                    after_open=self._after_knowledge_open,
                )
                await asyncio.to_thread(knowledge._load_descriptor)
                self._knowledges[name] = knowledge
            self._started = True


    async def close(self) -> None:
        """Close every knowledge database managed by this registry."""
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._started = False
            knowledges = list(self._knowledges.values())
            self._knowledges.clear()

        results = await asyncio.gather(
            *(knowledge.close() for knowledge in knowledges),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise RavenError(
                ErrorCode.INTERNAL_ERROR,
                "One or more knowledge databases could not be closed.",
                details={"failure_count": len(failures)},
            ) from failures[0]


    async def create(
        self,
        name: str,
        user_summary: str = "",
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.KNOWLEDGE_CREATE
        )
        return await active_operation.run(
            OperationType.KNOWLEDGE_CREATE,
            lambda active_operation: self._create(
                name,
                user_summary,
                operation=active_operation,
            ),
        )


    async def _create(
        self,
        name: str,
        user_summary: str = "",
        *,
        operation: Operation,
    ) -> Knowledge:
        """Create, persist, and register a new knowledge database."""
        self._ensure_started()
        safe_name = _safe_name(name)
        await self._emit(operation, EventType.KNOWLEDGE_CREATE_STARTED, {"name": safe_name})

        async with self._lifecycle_lock:
            if safe_name in self._knowledges or (self.knowledge_base_path / safe_name).exists():
                error = RavenError(
                    ErrorCode.KNOWLEDGE_ALREADY_EXISTS,
                    f"Knowledge '{name}' already exists.",
                )
                await self._emit(
                    operation,
                    EventType.KNOWLEDGE_CREATE_FAILED,
                    {"name": safe_name, "error": error_payload(error)},
                )
                raise error

            knowledge = await self._open(safe_name)
            try:
                if user_summary:
                    summary_task = await knowledge.set_summary(
                        user_summary,
                        operation=operation,
                    )
                    await summary_task.result()
            except Exception as exc:
                await knowledge.close()
                await asyncio.to_thread(shutil.rmtree, knowledge.dir_path, ignore_errors=True)
                await self._emit(
                    operation,
                    EventType.KNOWLEDGE_CREATE_FAILED,
                    {"name": safe_name, "error": error_payload(exc)},
                )
                raise
            self._knowledges[safe_name] = knowledge
            self._knowledges.move_to_end(safe_name)

        await self._emit(
            operation,
            EventType.KNOWLEDGE_CREATE_COMPLETED,
            {"name": safe_name, "user_summary": user_summary},
        )
        return knowledge


    def get(self, name: str) -> Knowledge:
        """Return an opened knowledge database by name."""
        self._ensure_started()
        safe_name = _safe_name(name)
        try:
            knowledge = self._knowledges[safe_name]
        except KeyError as exc:
            raise RavenError(
                ErrorCode.KNOWLEDGE_NOT_FOUND,
                f"Knowledge '{name}' does not exist.",
            ) from exc
        self._knowledges.move_to_end(safe_name)
        return knowledge


    async def list(self) -> list[dict[str, Any]]:
        """Return summaries of all opened knowledge databases."""
        self._ensure_started()
        return [
            {
                "name": knowledge.name,
                "safe_name": knowledge.safe_name,
                "user_summary": knowledge.get_summary(),
                "count": await knowledge.count(),
                "created_at": knowledge.meta.get("created_at"),
            }
            for knowledge in tuple(self._knowledges.values())
        ]


    async def reconcile_pending_ingestions(self) -> list[dict[str, Any]]:
        """Run final cleanup checks for active ingestion writes."""
        self._ensure_started()
        results: list[dict[str, Any]] = []
        for knowledge in tuple(self._knowledges.values()):
            try:
                results.extend(await knowledge.reconcile_pending_ingestions())
            except Exception:
                logger.exception(
                    "Final ingestion reconciliation failed for knowledge %s",
                    knowledge.name,
                )
                raise
        return results


    async def reconcile_pending_file_deletions(self) -> list[dict[str, Any]]:
        """Finish durable file deletions across all open knowledges."""
        self._ensure_started()
        results: list[dict[str, Any]] = []
        for knowledge in tuple(self._knowledges.values()):
            results.extend(await knowledge.reconcile_pending_file_deletions())
        return results


    async def validate_embedding_model(self, embed_model: Any) -> None:
        """Validate one embedding adapter against every persisted knowledge."""
        self._ensure_started()
        for knowledge in tuple(self._knowledges.values()):
            await knowledge.validate_embedding_model(embed_model)


    async def delete(
        self,
        name: str,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.KNOWLEDGE_DELETE
        )
        return await active_operation.run(
            OperationType.KNOWLEDGE_DELETE,
            lambda active_operation: self._delete(
                name,
                operation=active_operation,
            ),
        )


    async def _delete(
        self,
        name: str,
        *,
        operation: Operation,
    ) -> None:
        """Close, remove, and unregister a knowledge database."""
        self._ensure_started()
        safe_name = _safe_name(name)
        await self._emit(operation, EventType.KNOWLEDGE_DELETE_STARTED, {"name": safe_name})

        async with self._lifecycle_lock:
            knowledge = self.get(safe_name)
            try:
                await knowledge.close()
                await asyncio.to_thread(shutil.rmtree, knowledge.dir_path)
            except Exception as exc:
                await self._emit(
                    operation,
                    EventType.KNOWLEDGE_DELETE_FAILED,
                    {"name": safe_name, "error": error_payload(exc)},
                )
                raise
            self._knowledges.pop(safe_name, None)

        await self._emit(operation, EventType.KNOWLEDGE_DELETE_COMPLETED, {"name": safe_name})


    async def _open(self, name: str) -> Knowledge:
        knowledge = Knowledge(
            self.knowledge_base_path / name,
            operation_manager=self._operation_manager,
            before_open=self._before_knowledge_open,
            after_open=self._after_knowledge_open,
        )
        await knowledge.start()
        return knowledge


    async def _before_knowledge_open(self, target: Knowledge) -> None:
        async with self._resource_lock:
            if target.name in self._knowledges:
                self._knowledges.move_to_end(target.name)
            unavailable: set[int] = set()
            while True:
                opened = [
                    knowledge
                    for knowledge in self._knowledges.values()
                    if knowledge.is_started and knowledge is not target
                    and id(knowledge) not in self._opening_knowledges
                ]
                if (
                    len(opened) + len(self._opening_knowledges)
                    < self.max_open_knowledges
                ):
                    break
                candidate = next(
                    (
                        knowledge
                        for knowledge in opened
                        if knowledge.active_uses == 0
                        and id(knowledge) not in unavailable
                    ),
                    None,
                )
                if candidate is None:
                    break
                if not await candidate.release_resources():
                    unavailable.add(id(candidate))
            self._opening_knowledges.add(id(target))


    async def _after_knowledge_open(self, target: Knowledge) -> None:
        async with self._resource_lock:
            self._opening_knowledges.discard(id(target))


    def _scan_existing(self) -> list[str]:
        if not self.knowledge_base_path.is_dir():
            return []
        names: list[str] = []
        for entry in self.knowledge_base_path.iterdir():
            if not entry.is_dir() or not (entry / "meta.json").exists():
                continue
            try:
                metadata = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(metadata, dict)
                and metadata.get("schema_version", PERSISTENCE_VERSION) == PERSISTENCE_VERSION
                and metadata.get("name") == entry.name
            ):
                names.append(entry.name)
        return sorted(names)


    async def _emit(
        self,
        operation: Operation | None,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> None:
        if operation is not None:
            await operation.publish(Event(type=event_type, data=data or {}))


    def _ensure_started(self) -> None:
        if not self.is_started:
            raise RavenError(
                ErrorCode.KNOWLEDGE_NOT_STARTED,
                "Knowledge base is not started.",
            )
