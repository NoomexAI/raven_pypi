from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4, uuid5, UUID

import qdrant_client
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.ollama import OllamaEmbedding
from qdrant_client.http import models as qmodels

from ._paths import data_root
from .events import Event, EventBus, EventType

COLLECTION_NAME = "chunks"
PERSISTENCE_VERSION = 1

# Payload keys: Qdrant stores ONLY the vector + these IDs.
# Every text/metadata value lives in file.json (the relational side).
KEY_FILE_ID = "file_id"
KEY_SECTION_ID = "section_id"
KEY_CHUNK_INDEX = "chunk_index"

POINT_NS = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # DNS namespace, deterministic ids


def _point_id(file_id: str, section_index: int, chunk_index: int) -> str:
    return str(uuid5(POINT_NS, f"{file_id}:{section_index}:{chunk_index}"))


def _safe_name(name: str) -> str:
    import re

    s = re.sub(r"[^a-zA-Z0-9_\-]", "_", name)
    return s.strip("_") or "knowledge"


class Knowledge:
    """ONE self-contained knowledge database: a directory with its own embedded Qdrant store.

    The directory path is owned/assigned by the managing KnowledgeBase; a Knowledge
    never decides where it lives.
    """

    def __init__(
        self,
        name: str,
        dir_path: Path,
        bus: EventBus,
        embed_model: OllamaEmbedding,
        chunk_size: int = 512,
        chunk_overlap: int = 50,
    ) -> None:
        self.name = name
        self.bus = bus
        self.embed_model = embed_model
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

        self.dir_path = dir_path
        self.qdrant_dir = self.dir_path / "qdrant"
        self.meta_path = self.dir_path / "meta.json"
        self.files_path = self.dir_path / "file.json"

        self.dir_path.mkdir(parents=True, exist_ok=True)
        self.qdrant_dir.mkdir(parents=True, exist_ok=True)
        if not self.meta_path.exists():
            self._write_meta({})

        self._qdrant = qdrant_client.QdrantClient(path=str(self.qdrant_dir))
        self._file_meta: dict[str, Any] = self._load_file_meta()
        self._mutation_lock = asyncio.Lock()

    @property
    def safe_name(self) -> str:
        return _safe_name(self.name)

    # ---------- qdrant plumbing ----------

    def _collection_exists(self) -> bool:
        try:
            self._qdrant.get_collection(COLLECTION_NAME)
            return True
        except Exception:
            return False

    def _ensure_collection(self, dim: int) -> None:
        try:
            existing = self._qdrant.get_collection(COLLECTION_NAME)
        except Exception:
            existing = None
        if existing is None:
            self._qdrant.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
            )
        else:
            vectors = existing.config.params.vectors
            if isinstance(vectors, dict):
                sizes = [v.size for v in vectors.values() if v is not None]
                current = sizes[0] if sizes else None
            elif isinstance(vectors, qmodels.VectorParams):
                current = vectors.size
            else:
                current = None
            if current != dim:
                raise ValueError(
                    f"embedding dim mismatch for knowledge '{self.name}': "
                    f"collection has {current}, embed model produces {dim}"
                )

    @staticmethod
    def _filter(key: str, value: Any) -> Any:
        return qmodels.Filter(
            must=[qmodels.FieldCondition(key=key, match=qmodels.MatchValue(value=value))]
        )

    def _upsert_points(self, points: list[Any]) -> None:
        if not points:
            return
        self._qdrant.upsert(collection_name=COLLECTION_NAME, points=points, wait=True)

    # ---------- meta.json (knowledge-level scalars) ----------

    def _read_meta(self) -> dict[str, Any]:
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_meta(self, meta: dict[str, Any]) -> None:
        self._atomic_write(self.meta_path, meta)

    def _meta(self) -> dict[str, Any]:
        meta = self._read_meta()
        meta.setdefault("schema_version", PERSISTENCE_VERSION)
        meta.setdefault("name", self.name)
        meta.setdefault("created_at", datetime.now().isoformat())
        meta.setdefault("embed_model", self.embed_model.model_name)
        meta.setdefault("collection", COLLECTION_NAME)
        return meta

    def set_summary(self, summary: str) -> None:
        self._set_summary(summary)
        self.bus.publish(
            Event(
                type=EventType.KNOWLEDGE_UPDATED,
                data={"knowledge": self.name, "user_summary": summary},
            )
        )

    def _set_summary(self, summary: str) -> None:
        meta = self.meta
        meta["user_summary"] = summary
        self._write_meta(meta)

    def get_summary(self) -> str:
        return self.meta.get("user_summary", "")

    @property
    def meta(self) -> dict[str, Any]:
        return self._meta()

    # ---------- file.json (file registry + all section metadata) ----------

    def _load_file_meta(self) -> dict[str, Any]:
        try:
            return json.loads(self.files_path.read_text(encoding="utf-8"))
        except Exception:
            return {"files": {}, "sections": {}}

    def _save_file_meta(self, data: dict[str, Any]) -> None:
        self._atomic_write(self.files_path, data)
        self._file_meta = data

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _files_registry(self) -> dict[str, dict[str, Any]]:
        return self._file_meta.setdefault("files", {})

    def _sections_dict(self) -> dict[str, dict[str, Any]]:
        return self._file_meta.setdefault("sections", {})

    # ---------- embedding ----------

    def _split(self, text: str) -> list[str]:
        splitter = SentenceSplitter(chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)
        return splitter.split_text_metadata_aware(text, "")

    # ---------- ingest ----------

    async def ingest(
        self,
        file_name: str,
        sections: list[dict[str, Any]],
        op_id: str | None = None,
    ) -> str:
        op_id = op_id or uuid4().hex
        asyncio.create_task(self._ingest_task(file_name, sections, op_id))
        return op_id

    async def ingest_foreground(
        self,
        file_name: str,
        sections: list[dict[str, Any]],
        op_id: str | None = None,
    ) -> str:
        """Run storage ingestion in the caller's task.

        ``ingest`` remains compatible with the original background-task API;
        service operations use this method instead.
        """
        op_id = op_id or uuid4().hex
        await self._ingest_task(file_name, sections, op_id)
        return op_id

    async def _ingest_task(
        self,
        file_name: str,
        sections: list[dict[str, Any]],
        op_id: str,
    ) -> None:
        async with self._mutation_lock:
            await self._ingest_task_unlocked(file_name, sections, op_id)

    async def _ingest_task_unlocked(
        self,
        file_name: str,
        sections: list[dict[str, Any]],
        op_id: str,
    ) -> None:
        def emit(etype: EventType, data: dict[str, Any]) -> None:
            self.bus.publish(Event(type=etype, data=data, op_id=op_id))

        try:
            if self.file_exists(file_name):
                raise ValueError(f"file '{file_name}' already exists in knowledge '{self.name}'")

            file_id = uuid4().hex[:12]
            chunk_records: list[tuple[str, str, int, int, dict[str, Any]]] = []
            for section_index, section in enumerate(sections, start=1):
                texts = section.get("chunks") or self._split(section.get("raw_content", ""))
                for chunk_index, text in enumerate(texts):
                    point_id = _point_id(file_id, section_index, chunk_index)
                    chunk_records.append((point_id, text, section_index, chunk_index, section))

            doc_count = len(chunk_records)
            if doc_count == 0:
                raise ValueError(f"no chunks produced for file '{file_name}'")
            embed = self.embed_model
            vectors = await embed.aget_text_embedding_batch([r[1] for r in chunk_records])
            if doc_count:
                await asyncio.to_thread(self._ensure_collection, len(vectors[0]))
            points = [
                qmodels.PointStruct(
                    id=rid,
                    vector=list(vector),
                    payload={
                        KEY_FILE_ID: file_id,
                        KEY_SECTION_ID: f"{file_id}-{section_index}",
                        KEY_CHUNK_INDEX: chunk_index,
                    },
                )
                for (rid, _text, section_index, chunk_index, _section), vector in zip(
                    chunk_records, vectors
                )
            ]

            await asyncio.to_thread(self._upsert_points, points)
            chunk_total = len(points)

            original_file_meta = json.loads(json.dumps(self._file_meta))
            files = self._files_registry()
            sections_dict = self._sections_dict()
            files[file_name] = {
                "file_id": file_id,
                "sections": len(sections),
                "chunks": chunk_total,
                "ingested_at": time.time(),
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
                }
            try:
                await asyncio.to_thread(self._save_file_meta, self._file_meta)
            except Exception:
                self._file_meta = original_file_meta
                # Compensate for a successful vector write when the relational
                # registry cannot be committed.
                await asyncio.to_thread(
                    self._qdrant.delete,
                    collection_name=COLLECTION_NAME,
                    points_selector=qmodels.FilterSelector(
                        filter=self._filter(KEY_FILE_ID, file_id)
                    ),
                    wait=True,
                )
                raise

            emit(
                EventType.KNOWLEDGE_FILE_INGESTED,
                {"knowledge": self.name, "file": file_name, "count": chunk_total, "file_id": file_id},
            )
        except Exception as exc:
            emit(EventType.ERROR, {"knowledge": self.name, "file": file_name, "error": str(exc)})

    # ---------- inspection ----------

    def file_exists(self, file_name: str) -> bool:
        return file_name in self._files_registry()

    def list_files(self) -> list[dict[str, Any]]:
        result = []
        for file_name, info in self._files_registry().items():
            result.append(
                {
                    "file_id": info.get("file_id"),
                    "file_name": file_name,
                    "section_count": info.get("sections", 0),
                    "chunk_count": info.get("chunks", 0),
                    "ingested_at": info.get("ingested_at"),
                }
            )
        return result

    def list_sections(self, file_name: str) -> list[dict[str, Any]]:
        file_id = self._files_registry().get(file_name, {}).get("file_id")
        if file_id is None:
            return []
        sections = []
        for sid, s in self._sections_dict().items():
            if s.get("file_id") == file_id:
                sections.append({**s, "section_id": sid})
        sections.sort(key=lambda s: s.get("section_index", 0))
        return sections

    def get_section(self, section_id: str) -> dict[str, Any] | None:
        s = self._sections_dict().get(section_id)
        return {**s, "section_id": section_id} if s is not None else None

    def search(self, query_vector: list[float], top_k: int = 5) -> list[dict[str, Any]]:
        if not self._collection_exists():
            return []
        resp = self._qdrant.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            limit=top_k,
            with_payload=True,
        )
        return [
            {
                "file_id": (p.payload or {}).get(KEY_FILE_ID),
                "section_id": (p.payload or {}).get(KEY_SECTION_ID),
                "chunk_index": (p.payload or {}).get(KEY_CHUNK_INDEX),
                "score": p.score,
            }
            for p in resp.points
        ]

    def retrieve_context(self, query_vector: list[float], top_k: int = 5) -> list[dict[str, Any]]:
        """v1-shaped retrieval: top-k chunk hits -> whole sections from file.json."""
        hits = self.search(query_vector, top_k=top_k)
        sections_dict = self._sections_dict()
        seen: set[str] = set()
        result = []
        for hit in hits:
            sid = hit.get("section_id")
            if sid is None or sid in seen:
                continue
            section = sections_dict.get(sid)
            if section is None:
                continue
            seen.add(sid)
            result.append(
                {
                    "knowledge_name": self.name,
                    "file_name": section.get("file_name"),
                    "section_id": sid,
                    "raw_content": section.get("raw_content", ""),
                    "score": hit.get("score"),
                }
            )
        return result

    # ---------- deletion ----------

    def delete_file(self, file_id: str) -> None:
        file_name = next(
            (name for name, info in self._files_registry().items() if info.get("file_id") == file_id),
            None,
        )
        if self._collection_exists():
            self._qdrant.delete(
                collection_name=COLLECTION_NAME,
                points_selector=qmodels.FilterSelector(filter=self._filter(KEY_FILE_ID, file_id)),
                wait=True,
            )
        files = self._files_registry()
        self._file_meta["files"] = {
            name: info for name, info in files.items() if info.get("file_id") != file_id
        }
        self._file_meta["sections"] = {
            sid: s for sid, s in self._sections_dict().items() if s.get("file_id") != file_id
        }
        self._save_file_meta(self._file_meta)
        self.bus.publish(
            Event(
                type=EventType.KNOWLEDGE_FILE_DELETED,
                data={
                    "knowledge": self.name,
                    "file_id": file_id,
                    "file_name": file_name,
                },
            )
        )

    def count(self) -> int:
        if not self._collection_exists():
            return 0
        return self._qdrant.count(collection_name=COLLECTION_NAME, exact=True).count

    async def asearch(self, query_vector: list[float], top_k: int = 5) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.search, query_vector, top_k)

    async def acount(self) -> int:
        return await asyncio.to_thread(self.count)

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    def close(self) -> None:
        self._qdrant.close()


class KnowledgeBase:
    """Manager/registry of separate Knowledge databases.

    Sole owner of the knowledge_base_path (default <project>/data/knowledge_base).
    Each knowledge lives at knowledge_base_path/<name>; a Knowledge never knows
    where it is stored — the manager decides that.

    Construction is config-only: nothing is scanned or opened until ``await start()``.
    Everything that does real I/O (scan, Qdrant open/close, create, delete) is async
    and runs its blocking parts on a worker thread. In-memory accessors (get/list/
    set_summary/count) stay synchronous.
    """

    def __init__(
        self,
        bus: EventBus,
        knowledge_base_path: Path | None = None,
        embed_model: OllamaEmbedding | None = None,
    ) -> None:
        self.bus = bus
        self.knowledge_base_path = self._resolve_base_path(knowledge_base_path)
        self.knowledge_base_path.mkdir(parents=True, exist_ok=True)
        self.embed_model = embed_model
        self._knowledges: dict[str, Knowledge] = {}
        self._started = False

    @staticmethod
    def _resolve_base_path(knowledge_base_path: Path | None) -> Path:
        if knowledge_base_path is not None:
            return Path(knowledge_base_path).resolve()
        for var in ("RAVEN_DATA_DIR", "RAVEN_HOME"):
            val = os.environ.get(var)
            if val:
                return Path(val).resolve() / "knowledge_base"
        return data_root() / "knowledge_base"

    def _scan_existing(self) -> list[str]:
        """(blocking) Find valid knowledge dirs under the base path. Called on a worker thread."""
        result = []
        if not self.knowledge_base_path.is_dir():
            return result
        for entry in self.knowledge_base_path.iterdir():
            if not entry.is_dir():
                continue
            meta_path = entry / "meta.json"
            if not meta_path.exists():
                continue
            # Only treat dirs carrying a RAVEN knowledge meta (not a stray
            # qdrant/ or other folder that happens to contain meta.json).
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(meta, dict) and "name" in meta and "collection" in meta:
                result.append(_safe_name(entry.name))
        return result

    async def start(self) -> None:
        """Discover existing knowledge dirs and open them. Idempotent."""
        if self._started:
            return
        names = await asyncio.to_thread(self._scan_existing)
        for safe in names:
            if safe in self._knowledges:
                continue
            self._knowledges[safe] = await self._open(safe)
        self._started = True
        self.bus.publish(
            Event(type=EventType.LIFECYCLE_STARTED, data={"knowledge_base": str(self.knowledge_base_path)})
        )

    async def _open(self, name: str) -> Knowledge:
        if self.embed_model is None:
            raise ValueError(
                "KnowledgeBase has no embed model set; load one via "
                "ModelManager.load_embed_model() before calling start()"
            )
        # Knowledge construction blocks (mkdir, Qdrant open, file.json parse) -> run on worker thread.
        return await asyncio.to_thread(
            Knowledge,
            name=name,
            dir_path=self.knowledge_base_path / _safe_name(name),
            bus=self.bus,
            embed_model=self.embed_model,
        )

    async def create(self, name: str, user_summary: str = "") -> Knowledge:
        safe = _safe_name(name)
        if safe in self._knowledges:
            raise ValueError(f"knowledge '{name}' already exists")
        knowledge = await self._open(safe)
        await asyncio.to_thread(knowledge._set_summary, user_summary)
        self._knowledges[safe] = knowledge
        self.bus.publish(
            Event(
                type=EventType.KNOWLEDGE_CREATED,
                data={
                    "name": safe,
                    "path": str(knowledge.dir_path),
                    "user_summary": user_summary,
                },
            )
        )
        return knowledge

    def get(self, name: str) -> Knowledge:
        safe = _safe_name(name)
        if safe not in self._knowledges:
            raise KeyError(f"knowledge '{name}' does not exist")
        return self._knowledges[safe]

    def list(self) -> list[dict[str, Any]]:
        result = []
        for knowledge in self._knowledges.values():
            result.append(
                {
                    "name": knowledge.name,
                    "safe_name": knowledge.safe_name,
                    "user_summary": knowledge.get_summary(),
                    "count": knowledge.count(),
                    "embed_model": knowledge.meta.get("embed_model"),
                    "created_at": knowledge.meta.get("created_at"),
                }
            )
        return result

    async def alist(self) -> list[dict[str, Any]]:
        """Non-blocking knowledge listing for async retrieval/service paths."""
        return await asyncio.to_thread(self.list)

    async def delete(self, name: str) -> None:
        safe = _safe_name(name)
        if safe not in self._knowledges:
            raise KeyError(f"knowledge '{name}' does not exist")
        knowledge = self._knowledges.pop(safe)
        dir_path = knowledge.dir_path
        await asyncio.to_thread(KnowledgeBase._destroy, knowledge, dir_path)
        self.bus.publish(
            Event(
                type=EventType.KNOWLEDGE_DELETED,
                data={"name": safe, "path": str(dir_path)},
            )
        )

    @staticmethod
    def _destroy(knowledge: Knowledge, dir_path: Path) -> None:
        knowledge.close()
        shutil.rmtree(dir_path, ignore_errors=True)

    async def close(self) -> None:
        items = list(self._knowledges.values())
        self._knowledges.clear()

        def _shutdown() -> None:
            for knowledge in items:
                knowledge.close()

        await asyncio.to_thread(_shutdown)
        self.bus.publish(
            Event(type=EventType.LIFECYCLE_STOPPED, data={"knowledge_base": str(self.knowledge_base_path)})
        )
