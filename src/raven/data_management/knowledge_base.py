"""Knowledge database entities and their registry for Raven."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import shutil
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import qdrant_client
from llama_index.core.node_parser import SentenceSplitter
from qdrant_client.http import models as qmodels

from ..core.config import PathConfig
from ..core.errors import ErrorCode, RavenError, error_payload
from ..core.events import Event, EventType
from ..core.operations import Operation, OperationManager, OperationTask, OperationType

COLLECTION_NAME = "chunks"
PERSISTENCE_VERSION = 1

KEY_FILE_ID = "file_id"
KEY_SECTION_ID = "section_id"
KEY_CHUNK_INDEX = "chunk_index"

POINT_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

logger = logging.getLogger(__name__)


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


class Knowledge:
    """One self-contained knowledge database."""

    def __init__(
        self,
        dir_path: Path,
        *,
        operation_manager: OperationManager,
    ) -> None:
        self.dir_path = Path(dir_path)
        self.name = self.dir_path.name
        self.qdrant_dir = self.dir_path / "qdrant"
        self.meta_path = self.dir_path / "meta.json"
        self.files_path = self.dir_path / "file.json"

        self._qdrant: qdrant_client.QdrantClient | None = None
        self._meta: dict[str, Any] = {}
        self._file_meta: dict[str, Any] = {"schema_version": PERSISTENCE_VERSION, "files": {}, "sections": {}}
        self._lifecycle_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self._pending_ingestions: set[str] = set()
        self._started = False
        self._closed = False
        self._operation_manager = operation_manager


    @property
    def safe_name(self) -> str:
        return self.name


    @property
    def is_started(self) -> bool:
        return self._started and not self._closed


    @property
    def meta(self) -> dict[str, Any]:
        self._ensure_started()
        return copy.deepcopy(self._meta)


    async def start(self) -> None:
        """Open this knowledge database and load its metadata."""
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


    async def close(self) -> None:
        """Close the local Qdrant client."""
        if self.is_started:
            await self.reconcile_pending_ingestions()

        async with self._lifecycle_lock:
            if self._closed:
                return

            qdrant = self._qdrant
            self._qdrant = None
            self._closed = True
            self._started = False
            if qdrant is not None:
                await asyncio.to_thread(qdrant.close)


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
            lambda active_operation: self._set_summary(
                summary,
                operation=active_operation,
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
        self._ensure_started()
        return str(self._meta.get("user_summary", ""))


    def file_exists(self, file_name: str) -> bool:
        self._ensure_started()
        return file_name in self._files_registry()


    def list_files(self) -> list[dict[str, Any]]:
        self._ensure_started()
        result: list[dict[str, Any]] = []
        for file_name, info in self._files_registry().items():
            result.append(
                {
                    "file_id": info.get("file_id"),
                    "file_name": file_name,
                    "section_count": info.get("sections", 0),
                    "chunk_count": info.get("chunks", 0),
                    "ingested_at": info.get("ingested_at"),
                    "navigation_type": info.get("navigation_type", "none"),
                }
            )
        return result


    def list_sections(self, file_name: str) -> list[dict[str, Any]]:
        self._ensure_started()
        file_id = self._files_registry().get(file_name, {}).get("file_id")
        if file_id is None:
            return []

        sections = [
            {**section, "section_id": section_id}
            for section_id, section in self._sections_dict().items()
            if section.get("file_id") == file_id
        ]
        sections.sort(key=lambda section: section.get("section_index", 0))
        return sections


    def get_section(self, section_id: str) -> dict[str, Any] | None:
        self._ensure_started()
        section = self._sections_dict().get(section_id)
        if section is None:
            return None
        return {**section, "section_id": section_id}


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
            lambda active_operation: self._ingest(
                file_name,
                sections,
                file_id=file_id,
                embed_model=embed_model,
                navigation_type=navigation_type,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                operation=active_operation,
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
            lambda active_operation: self._reconcile_ingestion(
                file_id,
                operation=active_operation,
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
                committed = self._file_result_by_id(file_id)
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
        self._ensure_started()
        results: list[dict[str, Any]] = []

        async with self._mutation_lock:
            for file_id in tuple(self._pending_ingestions):
                committed = self._file_result_by_id(file_id)
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
            original_file_meta = copy.deepcopy(self._file_meta)
            vectors_written = False
            metadata_written = False
            cleanup_completed = False

            try:
                committed = self._file_result_by_id(file_id)
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

                if self.file_exists(file_name):
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
                    vectors_written = True
                    raise
                vectors_written = True
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

                self._file_meta = copy.deepcopy(original_file_meta)
                files = self._files_registry()
                sections_dict = self._sections_dict()
                files[file_name] = {
                    "file_id": file_id,
                    "sections": len(sections),
                    "chunks": len(points),
                    "ingested_at": datetime.now(timezone.utc).isoformat(),
                    "navigation_type": navigation_type,
                }
                for section_index, section in enumerate(sections, start=1):
                    section_id = f"{file_id}-{section_index}"
                    sections_dict[section_id] = {
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
                    }

                metadata_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._write_json,
                        self.files_path,
                        self._file_meta,
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
                    self._file_meta = original_file_meta
                    await asyncio.to_thread(self._delete_points_by_file_id, file_id)
                    cleanup_completed = True
                raise
            except Exception as exc:
                if not metadata_written:
                    self._file_meta = original_file_meta
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
        self._ensure_started()
        if top_k <= 0:
            return []
        return await asyncio.to_thread(self._search, query_vector, top_k)


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
            section = self.get_section(section_id)
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
        self._ensure_started()
        return await asyncio.to_thread(self._count)


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
            lambda active_operation: self._delete_file(
                file_id,
                operation=active_operation,
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
            original_file_meta = copy.deepcopy(self._file_meta)
            file_name = next(
                (
                    name
                    for name, info in self._files_registry().items()
                    if info.get("file_id") == file_id
                ),
                None,
            )
            if file_name is None:
                raise RavenError(
                    ErrorCode.FILE_NOT_FOUND,
                    f"File '{file_id}' does not exist in knowledge '{self.name}'.",
                )

            self._file_meta["files"] = {
                name: info
                for name, info in self._files_registry().items()
                if info.get("file_id") != file_id
            }
            self._file_meta["sections"] = {
                section_id: section
                for section_id, section in self._sections_dict().items()
                if section.get("file_id") != file_id
            }

            try:
                await asyncio.to_thread(self._write_json, self.files_path, self._file_meta)
                await asyncio.to_thread(self._delete_points_by_file_id, file_id)
            except Exception as exc:
                self._file_meta = original_file_meta
                await asyncio.to_thread(self._write_json, self.files_path, original_file_meta)
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


    def _open_storage(self) -> None:
        self.dir_path.mkdir(parents=True, exist_ok=True)
        self.qdrant_dir.mkdir(parents=True, exist_ok=True)
        self._meta = self._load_meta()
        self._file_meta = self._load_file_meta()
        self._qdrant = qdrant_client.QdrantClient(path=str(self.qdrant_dir))


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


    def _load_file_meta(self) -> dict[str, Any]:
        if not self.files_path.exists():
            data = {"schema_version": PERSISTENCE_VERSION, "files": {}, "sections": {}}
            self._write_json(self.files_path, data)
            return data

        data = self._read_json(self.files_path)
        if not isinstance(data, dict):
            raise RavenError(
                ErrorCode.INVALID_METADATA,
                f"Invalid file metadata for knowledge '{self.name}'.",
            )
        if data.get("schema_version", PERSISTENCE_VERSION) != PERSISTENCE_VERSION:
            raise RavenError(
                ErrorCode.UNSUPPORTED_METADATA_VERSION,
                f"Unsupported file metadata version for knowledge '{self.name}'.",
            )
        data.setdefault("files", {})
        data.setdefault("sections", {})
        return data


    def _files_registry(self) -> dict[str, dict[str, Any]]:
        return self._file_meta.setdefault("files", {})


    def _sections_dict(self) -> dict[str, dict[str, Any]]:
        return self._file_meta.setdefault("sections", {})


    def _file_result_by_id(self, file_id: str) -> dict[str, Any] | None:
        for file_name, info in self._files_registry().items():
            if info.get("file_id") != file_id:
                continue
            return {
                "knowledge": self.name,
                "file": file_name,
                "file_id": file_id,
                "section_count": int(info.get("sections", 0)),
                "chunk_count": int(info.get("chunks", 0)),
            }
        return None


    def _split(self, text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
        splitter = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        return splitter.split_text_metadata_aware(text, "")


    def _collection_exists(self) -> bool:
        qdrant = self._require_qdrant()
        try:
            qdrant.get_collection(COLLECTION_NAME)
        except Exception:
            return False
        return True


    def _ensure_collection(self, dimension: int) -> None:
        qdrant = self._require_qdrant()
        try:
            existing = qdrant.get_collection(COLLECTION_NAME)
        except Exception:
            existing = None

        if existing is None:
            qdrant.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=qmodels.VectorParams(
                    size=dimension,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            return

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
    ) -> None:
        self.knowledge_base_path = paths.knowledge_base_dir
        self._knowledges: dict[str, Knowledge] = {}
        self._lifecycle_lock = asyncio.Lock()
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
            opened: list[Knowledge] = []
            try:
                for name in names:
                    knowledge = await self._open(name)
                    self._knowledges[name] = knowledge
                    opened.append(knowledge)
            except Exception:
                await asyncio.gather(*(knowledge.close() for knowledge in opened))
                self._knowledges.clear()
                raise
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

        await asyncio.gather(*(knowledge.close() for knowledge in knowledges))


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
            return self._knowledges[safe_name]
        except KeyError as exc:
            raise RavenError(
                ErrorCode.KNOWLEDGE_NOT_FOUND,
                f"Knowledge '{name}' does not exist.",
            ) from exc


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
            for knowledge in self._knowledges.values()
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
        )
        await knowledge.start()
        return knowledge


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
