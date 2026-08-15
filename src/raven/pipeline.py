from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from uuid import uuid4

from llama_index.core import Document
from llama_index.core.node_parser import SemanticSplitterNodeParser
from llama_index.core.prompts.base import ChatPromptTemplate
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.llms.ollama import Ollama
from pydantic import BaseModel, Field

from .events import Event, EventBus, EventType
from .knowledge import Knowledge, KnowledgeBase

logger = logging.getLogger(__name__)

MAX_EXTRACTION_RETRIES = 3

METADATA_SYSTEM_PROMPT = """\
You are a precise data extraction agent. You are given ONE section of a document and must produce its metadata as strict JSON.

Fields:
- summary: a concise summary of the section's main concept. Can be multiple sentences if necessary.
- keywords: a list of relevant tags/identifiers found in this text segment.
- conditions: an explicit list of conditional states, numeric thresholds (e.g. temperatures, voltages, chemical values, or conditional events), or functional constraints that trigger actions. Leave empty if none.
- definitions: short answers to the "what" and "who" questions answerable from this section.

OUTPUT: ONLY a valid JSON object matching the schema. No conversational text, no markdown wrappers, no explanations.
"""


class SectionMetadata(BaseModel):
    summary: str = Field(description="Concise summary of the section's main concept")
    keywords: list[str] = Field(default_factory=list, description="Relevant tags/identifiers in this segment")
    conditions: list[str] = Field(
        default_factory=list,
        description="Conditional states, thresholds, or functional constraints that trigger actions",
    )
    definitions: list[str] = Field(
        default_factory=list,
        description="Answers to the what/who questions answerable from this section",
    )


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


class IngestionPipeline:
    """Semantic sectioning + LLM metadata extraction → Knowledge.ingest.

    Replaces v1's tokenizer subdivision, SBD model, and grammar enforcement:
    - SemanticSplitterNodeParser groups a file into semantically-coherent sections (embedding-driven).
    - One schema-enforced LLM call per section produces summary/keywords/conditions/definitions
      (Ollama enforces the JSON schema via its `format` field — no llama_cpp grammar).
    - Sections feed Knowledge.ingest, which owns chunking-for-embedding + storage.

    Owns the `ingestion.*` events (progress / complete / error) on the bus, with the op_id contract.
    """

    def __init__(
        self,
        bus: EventBus,
        knowledge_base: KnowledgeBase,
        llm: Ollama,
        embed_model: OllamaEmbedding,
        breakpoint_percentile_threshold: int = 95,
        buffer_size: int = 1,
        max_extraction_retries: int = MAX_EXTRACTION_RETRIES,
    ) -> None:
        self._bus = bus
        self._knowledge_base = knowledge_base
        self._llm = llm
        self._embed_model = embed_model
        self._max_extraction_retries = max_extraction_retries
        self.base_model = llm.model
        self.embed_model = embed_model.model_name

        self._splitter = SemanticSplitterNodeParser(
            embed_model=embed_model,
            breakpoint_percentile_threshold=breakpoint_percentile_threshold,
            buffer_size=buffer_size,
        )
        self._prompt = ChatPromptTemplate.from_messages(
            [
                ("system", METADATA_SYSTEM_PROMPT),
                ("user", "SECTION TEXT:\n{section_text}"),
            ]
        )

    async def ingest(self, knowledge_name: str, file_path: str, op_id: str | None = None) -> str:
        op_id = op_id or uuid4().hex
        asyncio.create_task(self._ingest_task(knowledge_name, file_path, op_id))
        return op_id

    async def _extract_metadata(self, section_text: str) -> SectionMetadata | None:
        for attempt in range(1, self._max_extraction_retries + 1):
            try:
                return await self._llm.astructured_predict(
                    output_cls=SectionMetadata,
                    prompt=self._prompt,
                    section_text=section_text,
                )
            except Exception as exc:  # network / schema / decode failures
                logger.warning(f"metadata extraction attempt {attempt} failed: {exc}")
                if attempt == self._max_extraction_retries:
                    return None
        return None

    async def _ingest_task(self, knowledge_name: str, file_path: str, op_id: str) -> None:
        file_name = os.path.basename(file_path)

        def emit(etype: EventType, data: dict) -> None:
            self._bus.publish(Event(type=etype, data=data, op_id=op_id))

        def progress(stage: str, nodes_done: int, total: int) -> None:
            emit(
                EventType.INGESTION_PROGRESS,
                {
                    "knowledge": knowledge_name,
                    "file": file_name,
                    "stage": stage,
                    "nodes_done": nodes_done,
                    "total": total,
                },
            )

        try:
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"File: {file_path} does not exist")

            knowledge = self._knowledge_base.get(knowledge_name)

            logger.info(f"Ingestion started: {knowledge_name} <- {file_path}")
            text = await asyncio.to_thread(_read_text, file_path)
            nodes = await self._splitter.aget_nodes_from_documents([Document(text=text)])
            total = len(nodes)
            progress(stage="sectioning", nodes_done=total, total=total)

            sections: list[dict] = []
            for i, node in enumerate(nodes, start=1):
                progress(stage="extracting", nodes_done=i - 1, total=total)
                node_text = node.get_content()
                metadata = await self._extract_metadata(node_text)
                if metadata is None:
                    logger.error(f"section {i}/{total} skipped after {self._max_extraction_retries} failed attempts")
                    continue
                sections.append(
                    {
                        "summary": metadata.summary,
                        "keywords": metadata.keywords,
                        "conditions": metadata.conditions,
                        "definitions": metadata.definitions,
                        "raw_content": node_text,
                    }
                )

            if not sections:
                raise RuntimeError(f"no sections produced for file '{file_name}'")

            progress(stage="ingesting", nodes_done=len(sections), total=total)
            storage_op = await knowledge.ingest(file_name, sections)

            count: int | None = None
            for ev in self._bus.history(op_id=storage_op):
                if ev.type is EventType.ERROR:
                    raise RuntimeError(ev.data.get("error", "storage ingest failed"))
                if ev.type is EventType.KNOWLEDGE_FILE_INGESTED:
                    count = ev.data.get("count", 0)
                    break
            if count is None:
                async for ev in self._bus.subscribe(op_id=storage_op):
                    if ev.type is EventType.ERROR:
                        raise RuntimeError(ev.data.get("error", "storage ingest failed"))
                    if ev.type is EventType.KNOWLEDGE_FILE_INGESTED:
                        count = ev.data.get("count", 0)
                        break

            emit(EventType.INGESTION_COMPLETE, {"knowledge": knowledge_name, "file": file_name, "count": count})
            logger.info(f"Ingestion complete: {file_name} ({count} chunks)")
        except Exception as exc:
            emit(EventType.ERROR, {"knowledge": knowledge_name, "file": file_name, "error": str(exc)})
            logger.error(f"Ingestion failed: {exc}")


# =====================================================================
# Retrieval Pipelines (v1 _pipeline.py ported to llama-index + Qdrant)
# =====================================================================

# --- retrieval modes (v1 naming: local/global x embedded/hierarchical/agreement/vector-conditioned) ---
LOCAL_EMBEDDED_RETRIEVAL = "local_embedded_retrieval"
LOCAL_HIERARCHICAL_RETRIEVAL = "local_hierarchical_retrieval"
LOCAL_AGREEMENT_RETRIEVAL = "local_agreement_retrieval"
LOCAL_VECTOR_CONDITIONED_RETRIEVAL = "local_vector_conditioned_retrieval"
GLOBAL_EMBEDDED_RETRIEVAL = "global_embedded_retrieval"
GLOBAL_HIERARCHICAL_RETRIEVAL = "global_hierarchical_retrieval"
GLOBAL_AGREEMENT_RETRIEVAL = "global_agreement_retrieval"
GLOBAL_VECTOR_CONDITIONED_RETRIEVAL = "global_vector_conditioned_retrieval"
HIERARCHICAL_BY_FILE = "hierarchical_by_file"
HIERARCHICAL_BY_KNOWLEDGE = "hierarchical_by_knowledge"

MAX_SCORING_RETRIES = 3

# Each retrieval mode maps to its own event types (started / read / completed).
# read is only meaningful for the hierarchical modes (scored reads).
RETRIEVAL_STARTED_FOR_MODE: dict[str, EventType] = {
    LOCAL_EMBEDDED_RETRIEVAL: EventType.RETRIEVAL_EMBEDDED_STARTED,
    GLOBAL_EMBEDDED_RETRIEVAL: EventType.RETRIEVAL_EMBEDDED_STARTED,
    LOCAL_HIERARCHICAL_RETRIEVAL: EventType.RETRIEVAL_HIERARCHICAL_STARTED,
    GLOBAL_HIERARCHICAL_RETRIEVAL: EventType.RETRIEVAL_HIERARCHICAL_STARTED,
    HIERARCHICAL_BY_FILE: EventType.RETRIEVAL_HIERARCHICAL_STARTED,
    HIERARCHICAL_BY_KNOWLEDGE: EventType.RETRIEVAL_HIERARCHICAL_STARTED,
    LOCAL_AGREEMENT_RETRIEVAL: EventType.RETRIEVAL_AGREEMENT_STARTED,
    GLOBAL_AGREEMENT_RETRIEVAL: EventType.RETRIEVAL_AGREEMENT_STARTED,
    LOCAL_VECTOR_CONDITIONED_RETRIEVAL: EventType.RETRIEVAL_VECTOR_CONDITIONED_STARTED,
    GLOBAL_VECTOR_CONDITIONED_RETRIEVAL: EventType.RETRIEVAL_VECTOR_CONDITIONED_STARTED,
}

RETRIEVAL_READ_FOR_MODE: dict[str, EventType] = {
    LOCAL_HIERARCHICAL_RETRIEVAL: EventType.RETRIEVAL_HIERARCHICAL_READ,
    GLOBAL_HIERARCHICAL_RETRIEVAL: EventType.RETRIEVAL_HIERARCHICAL_READ,
    HIERARCHICAL_BY_FILE: EventType.RETRIEVAL_HIERARCHICAL_READ,
    HIERARCHICAL_BY_KNOWLEDGE: EventType.RETRIEVAL_HIERARCHICAL_READ,
}

RETRIEVAL_COMPLETED_FOR_MODE: dict[str, EventType] = {
    LOCAL_EMBEDDED_RETRIEVAL: EventType.RETRIEVAL_EMBEDDED_COMPLETED,
    GLOBAL_EMBEDDED_RETRIEVAL: EventType.RETRIEVAL_EMBEDDED_COMPLETED,
    LOCAL_HIERARCHICAL_RETRIEVAL: EventType.RETRIEVAL_HIERARCHICAL_COMPLETED,
    GLOBAL_HIERARCHICAL_RETRIEVAL: EventType.RETRIEVAL_HIERARCHICAL_COMPLETED,
    HIERARCHICAL_BY_FILE: EventType.RETRIEVAL_HIERARCHICAL_COMPLETED,
    HIERARCHICAL_BY_KNOWLEDGE: EventType.RETRIEVAL_HIERARCHICAL_COMPLETED,
    LOCAL_AGREEMENT_RETRIEVAL: EventType.RETRIEVAL_AGREEMENT_COMPLETED,
    GLOBAL_AGREEMENT_RETRIEVAL: EventType.RETRIEVAL_AGREEMENT_COMPLETED,
    LOCAL_VECTOR_CONDITIONED_RETRIEVAL: EventType.RETRIEVAL_VECTOR_CONDITIONED_COMPLETED,
    GLOBAL_VECTOR_CONDITIONED_RETRIEVAL: EventType.RETRIEVAL_VECTOR_CONDITIONED_COMPLETED,
}


class ScoredSection(BaseModel):
    section_id: str = Field(description="a section id from the provided batch")
    score: int = Field(description="relevance score on the 0-10 integer scale used by the anchors")


class SectionScoreOutput(BaseModel):
    scores: list[ScoredSection] = Field(description="one score entry per section in the batch")


class ScoredKnowledge(BaseModel):
    knowledge_name: str = Field(description="a knowledge name from the provided batch")
    score: int = Field(description="relevance score on the 0-10 integer scale used by the anchors")


class KnowledgeScoreOutput(BaseModel):
    scores: list[ScoredKnowledge] = Field(description="one score entry per knowledge in the batch")


SCORE_SYSTEM_PROMPT = """\
You are a precise retrieval scorer. You score document SECTIONS by relevance to a user query.

Sections are provided as a JSON array of their METADATA only (no full content): section_id, file_name, summary, keywords, conditions, definitions. Judge relevance from this metadata.

Some already-scored sections are given as ANCHORS with their score (0-10). Use them to calibrate: if a new section is about as relevant as an anchor scored 8, score the new one 8. The context resets between reads, so the anchors are the ONLY way scores stay comparable across reads.

RULES:
- Scores are integers 0-10. 10 = directly answers the query; 0 = irrelevant.
- Score EVERY section in the JSON array. Order is not meaningful.
- Only return the score object. No prose, no markdown.
"""

KNOWLEDGE_SCORE_SYSTEM_PROMPT = """\
You are a precise retrieval scorer. You score KNOWLEDGE databases by relevance to a user query.

Knowledges are provided as a JSON array. Each item has a knowledge_name and its user summary. Judge relevance from the summary; you do not see the full contents here.

Some already-scored knowledges are given as ANCHORS with their score (0-10). Use them to calibrate: if a new knowledge is about as relevant as an anchor scored 8, score the new one 8. The context resets between reads, so the anchors are the ONLY way scores stay comparable across reads.

RULES:
- Scores are integers 0-10. 10 = directly answers the query; 0 = irrelevant.
- Score EVERY knowledge in the JSON array. Order is not meaningful.
- Only return the score object. No prose, no markdown.
"""


class RetrievalPipeline:
    """Shared data access for all retrieval pipelines.

    Owns no models — subclasses receive the loaded model objects directly.
    Publishes retrieval.* events on the bus (async + observable, per plan).
    Every public method returns the unscored section list
    [{knowledge_name, file_name, section_id, raw_content}], except the
    agreement pipeline which returns {agreement_type, retrieved_content}.
    """

    def __init__(self, knowledge_base: KnowledgeBase, bus: EventBus) -> None:
        self._kb = knowledge_base
        self._bus = bus

    def _emit(self, etype: EventType, data: dict, op_id: str) -> None:
        self._bus.publish(Event(type=etype, data=data, op_id=op_id))

    @staticmethod
    def _trim_output(selected: list[dict]) -> list[dict]:
        return [
            {
                "knowledge_name": c["knowledge_name"],
                "file_name": c["file_name"],
                "section_id": c["section_id"],
                "raw_content": c["raw_content"],
            }
            for c in selected
        ]

    def _knowledge_names(self) -> list[str]:
        return [k["safe_name"] for k in self._kb.list()]

    def _sections_of(self, knowledge_name: str) -> list[dict]:
        """All section dicts of one knowledge (cached file.json reads; adds knowledge_name)."""
        knowledge = self._kb.get(knowledge_name)
        sections = []
        for f in knowledge.list_files():
            for s in knowledge.list_sections(f["file_name"]):
                s["knowledge_name"] = knowledge_name
                sections.append(s)
        return sections

    def _knowledge_summary(self, knowledge_name: str) -> str:
        return self._kb.get(knowledge_name).get_summary()


class EmbeddedRetrievalPipeline(RetrievalPipeline):
    """Pure vector retrieval. Query -> embed -> Qdrant top-k hits -> whole sections."""

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        embed_model: OllamaEmbedding,
        bus: EventBus,
    ) -> None:
        super().__init__(knowledge_base, bus)
        self._embed = embed_model


    async def retrieve_local_context(
        self,
        knowledge_name: str,
        user_query: str,
        top_k: int = 3,
        op_id: str | None = None,
    ) -> list[dict]:

        op_id = op_id or uuid4().hex
        self._emit(
            RETRIEVAL_STARTED_FOR_MODE[LOCAL_EMBEDDED_RETRIEVAL],
            {"mode": LOCAL_EMBEDDED_RETRIEVAL, "query": user_query, "top_k": top_k, "knowledge": knowledge_name},
            op_id,
        )

        try:
            vec = await self._embed.aget_text_embedding(user_query)
            knowledge = self._kb.get(knowledge_name)
            hits = knowledge.search(vec, top_k=top_k)

            result: list[dict] = []
            seen: set[str] = set()
            for h in hits:
                sid = h.get("section_id")
                if not sid or sid in seen:
                    continue

                s = knowledge.get_section(sid)
                if s is None:
                    continue

                seen.add(sid)
                result.append(
                    {
                        "knowledge_name": knowledge_name,
                        "file_name": s["file_name"],
                        "section_id": sid,
                        "raw_content": s["raw_content"],
                    }
                )

            self._emit(
                RETRIEVAL_COMPLETED_FOR_MODE[LOCAL_EMBEDDED_RETRIEVAL],
                {
                    "mode": LOCAL_EMBEDDED_RETRIEVAL,
                    "count": len(result),
                    "section_ids": [r["section_id"] for r in result],
                    "knowledge": knowledge_name,
                },
                op_id,
            )

            return result
        
        except Exception as exc:
            self._emit(
                EventType.ERROR,
                {"op": "retrieval", "mode": LOCAL_EMBEDDED_RETRIEVAL, "error": str(exc), "knowledge": knowledge_name},
                op_id,
            )
            raise

    async def retrieve_global_context(
        self,
        user_query: str,
        top_k: int = 3,
        op_id: str | None = None,
    ) -> list[dict]:
        
        op_id = op_id or uuid4().hex
        self._emit(
            RETRIEVAL_STARTED_FOR_MODE[GLOBAL_EMBEDDED_RETRIEVAL],
            {"mode": GLOBAL_EMBEDDED_RETRIEVAL, "query": user_query, "top_k": top_k},
            op_id,
        )

        try:
            vec = await self._embed.aget_text_embedding(user_query)
            merged: list[dict] = []
            for name in self._knowledge_names():
                try:
                    hits = self._kb.get(name).search(vec, top_k=top_k)
                except Exception:
                    continue
                for h in hits:
                    merged.append({"knowledge_name": name, **h})
            merged.sort(key=lambda x: x.get("score", 0.0), reverse=True)

            result: list[dict] = []
            seen: set[tuple[str, str]] = set()
            for row in merged:
                sid = row.get("section_id")
                key = (row["knowledge_name"], sid or "")
                if not sid or key in seen:
                    continue
                section = self._kb.get(row["knowledge_name"]).get_section(sid)
                if section is None:
                    continue
                seen.add(key)
                result.append(
                    {
                        "knowledge_name": row["knowledge_name"],
                        "file_name": section["file_name"],
                        "section_id": sid,
                        "raw_content": section["raw_content"],
                    }
                )
                if len(result) >= top_k:
                    break
            self._emit(
                RETRIEVAL_COMPLETED_FOR_MODE[GLOBAL_EMBEDDED_RETRIEVAL],
                {
                    "mode": GLOBAL_EMBEDDED_RETRIEVAL,
                    "count": len(result),
                    "section_ids": [r["section_id"] for r in result],
                },
                op_id,
            )
            return result
        except Exception as exc:
            self._emit(
                EventType.ERROR,
                {"op": "retrieval", "mode": GLOBAL_EMBEDDED_RETRIEVAL, "error": str(exc)},
                op_id,
            )
            raise


class HierarchicalRetrievalPipeline(RetrievalPipeline):
    """LLM-over-metadata retrieval via anchored sequential scored reads.

    The candidate metadata is scored in batches of ``max_sections_per_read``.
    The context resets between reads; only the current top ``anchor_count``
    scored candidates are carried forward as calibration anchors so scores stay
    comparable across reads. Final ranking = aggregate scores, pure top-k.
    """

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        llm: Ollama,
        bus: EventBus,
        max_retries: int = MAX_SCORING_RETRIES,
    ) -> None:
        super().__init__(knowledge_base, bus)
        self._llm = llm
        self._max_retries = max_retries
        self._section_prompt = ChatPromptTemplate.from_messages(
            [
                ("system", SCORE_SYSTEM_PROMPT),
                (
                    "user",
                    "USER QUERY: {user_query}\n\n"
                    "ANCHORS (previously scored sections, same 0-10 scale):\n{anchors}\n\n"
                    "SECTIONS TO SCORE:\n{candidates}",
                ),
            ]
        )
        self._knowledge_prompt = ChatPromptTemplate.from_messages(
            [
                ("system", KNOWLEDGE_SCORE_SYSTEM_PROMPT),
                (
                    "user",
                    "USER QUERY: {user_query}\n\n"
                    "ANCHORS (previously scored knowledges, same 0-10 scale):\n{anchors}\n\n"
                    "KNOWLEDGES TO SCORE:\n{candidates}",
                ),
            ]
        )

    # ---------- anchored scored reads ----------

    async def _scored_select(
        self,
        user_query: str,
        candidates: list[dict],
        top_k: int,
        mode: str,
        op_id: str,
        prompt: ChatPromptTemplate,
        output_cls: type,
        id_field: str,
        max_per_read: int = 50,
        anchor_count: int = 4,
    ) -> list[dict]:

        if not candidates:
            return []

        batches = [
            candidates[i : i + max_per_read] for i in range(0, len(candidates), max_per_read)
        ]
        aggregated: dict[str, int] = {}
        order: list[str] = []

        for bi, batch in enumerate(batches, start=1):
            batch_ids = {c[id_field] for c in batch}
            ranked = sorted(aggregated.items(), key=lambda kv: kv[1], reverse=True)
            anchors = "\n".join(f"{sid}: score={score}/10" for sid, score in ranked[:anchor_count])
            anchors = anchors or "(none - first read)"

            # LLM scores on metadata only; raw_content is stripped from what it sees
            # but stays on the candidate for the final output mapping.
            meta_batch = [
                {k: v for k, v in c.items() if k != "raw_content"} for c in batch
            ]

            result: SectionScoreOutput | KnowledgeScoreOutput | None = None
            for attempt in range(1, self._max_retries + 1):
                try:
                    result = await self._llm.astructured_predict(
                        output_cls=output_cls,
                        prompt=prompt,
                        user_query=user_query,
                        anchors=anchors,
                        candidates=json.dumps(meta_batch, ensure_ascii=False),
                    )
                    break
                except Exception as exc:
                    logger.warning(f"{mode}: scored read {bi}/{len(batches)} attempt {attempt} failed: {exc}")
                    if attempt == self._max_retries:
                        raise
            if result is None:
                raise RuntimeError(f"{mode}: scored read {bi}/{len(batches)} failed after {self._max_retries} attempts")

            scored_this = 0
            for entry in result.scores:
                sid = getattr(entry, id_field, None)
                if sid is None or sid not in batch_ids or sid in aggregated:
                    continue
                score = max(0, min(10, int(entry.score)))
                aggregated[sid] = score
                order.append(sid)
                scored_this += 1

            self._emit(
                RETRIEVAL_READ_FOR_MODE[mode],
                {
                    "mode": mode,
                    "batch": bi,
                    "batch_total": len(batches),
                    "candidates": len(batch),
                    "scored": scored_this,
                },
                op_id,
            )
            if scored_this == 0:
                logger.warning(f"{mode}: read {bi}/{len(batches)} returned no valid scores")

        ranked = sorted(order, key=lambda sid: (-aggregated[sid], order.index(sid)))
        by_id = {c[id_field]: c for c in candidates}
        return [by_id[sid] for sid in ranked[:top_k]]

    async def _scored_sections(
        self,
        user_query: str,
        knowledge_names: list[str],
        top_k: int,
        mode: str,
        op_id: str,
        max_per_read: int,
        anchor_count: int,
    ) -> list[dict]:

        candidates: list[dict] = []
        for name in knowledge_names:
            candidates.extend(self._sections_of(name))

        selected = await self._scored_select(
            user_query, candidates, top_k, mode, op_id, self._section_prompt, SectionScoreOutput,
            id_field="section_id", max_per_read=max_per_read, anchor_count=anchor_count,
        )
        return self._trim_output(selected)

    # ---------- public entrypoints ----------

    async def retrieve_local_context(
        self,
        knowledge_name: str,
        user_query: str,
        top_k: int = 3,
        max_per_read: int = 50,
        anchor_count: int = 4,
        op_id: str | None = None,
    ) -> list[dict]:
        op_id = op_id or uuid4().hex
        self._emit(
            RETRIEVAL_STARTED_FOR_MODE[LOCAL_HIERARCHICAL_RETRIEVAL],
            {"mode": LOCAL_HIERARCHICAL_RETRIEVAL, "query": user_query, "top_k": top_k, "knowledge": knowledge_name},
            op_id,
        )
        try:
            result = await self._scored_sections(
                user_query, [knowledge_name], top_k, LOCAL_HIERARCHICAL_RETRIEVAL, op_id,
                max_per_read, anchor_count,
            )
            self._emit(
                RETRIEVAL_COMPLETED_FOR_MODE[LOCAL_HIERARCHICAL_RETRIEVAL],
                {
                    "mode": LOCAL_HIERARCHICAL_RETRIEVAL,
                    "count": len(result),
                    "section_ids": [r["section_id"] for r in result],
                    "knowledge": knowledge_name,
                },
                op_id,
            )
            return result
        except Exception as exc:
            self._emit(
                EventType.ERROR,
                {"op": "retrieval", "mode": LOCAL_HIERARCHICAL_RETRIEVAL, "error": str(exc), "knowledge": knowledge_name},
                op_id,
            )
            raise

    async def retrieve_global_context(
        self,
        user_query: str,
        top_k_section: int = 3,
        top_k_knowledge: int = 2,
        full_retrieval: bool = False,
        max_per_read: int = 50,
        anchor_count: int = 4,
        op_id: str | None = None,
    ) -> list[dict]:
        op_id = op_id or uuid4().hex
        self._emit(
            RETRIEVAL_STARTED_FOR_MODE[GLOBAL_HIERARCHICAL_RETRIEVAL],
            {"mode": GLOBAL_HIERARCHICAL_RETRIEVAL, "query": user_query, "top_k": top_k_section},
            op_id,
        )
        try:
            names = self._knowledge_names()
            if not full_retrieval:
                # Same anchored scored-read helper, over knowledge summaries.
                kcands = [
                    {"knowledge_name": n, "summary": self._knowledge_summary(n) or "(no summary)"}
                    for n in names
                ]
                selected_knowledge = await self._scored_select(
                    user_query, kcands, top_k_knowledge, GLOBAL_HIERARCHICAL_RETRIEVAL, op_id,
                    self._knowledge_prompt, KnowledgeScoreOutput, id_field="knowledge_name",
                    max_per_read=max_per_read, anchor_count=anchor_count,
                )
                names = [c["knowledge_name"] for c in selected_knowledge]
            result = await self._scored_sections(
                user_query, names, top_k_section, GLOBAL_HIERARCHICAL_RETRIEVAL, op_id,
                max_per_read, anchor_count,
            )
            self._emit(
                RETRIEVAL_COMPLETED_FOR_MODE[GLOBAL_HIERARCHICAL_RETRIEVAL],
                {
                    "mode": GLOBAL_HIERARCHICAL_RETRIEVAL,
                    "count": len(result),
                    "section_ids": [r["section_id"] for r in result],
                },
                op_id,
            )
            return result
        except Exception as exc:
            self._emit(
                EventType.ERROR,
                {"op": "retrieval", "mode": GLOBAL_HIERARCHICAL_RETRIEVAL, "error": str(exc)},
                op_id,
            )
            raise

    async def retrieve_by_knowledge(
        self,
        user_query: str,
        knowledge_name_list: list[str],
        top_k_section: int = 5,
        max_per_read: int = 50,
        anchor_count: int = 4,
        op_id: str | None = None,
    ) -> list[dict]:
        op_id = op_id or uuid4().hex
        self._emit(
            RETRIEVAL_STARTED_FOR_MODE[HIERARCHICAL_BY_KNOWLEDGE],
            {"mode": HIERARCHICAL_BY_KNOWLEDGE, "query": user_query, "top_k": top_k_section},
            op_id,
        )
        try:
            result = await self._scored_sections(
                user_query, knowledge_name_list, top_k_section, HIERARCHICAL_BY_KNOWLEDGE, op_id,
                max_per_read, anchor_count,
            )
            self._emit(
                RETRIEVAL_COMPLETED_FOR_MODE[HIERARCHICAL_BY_KNOWLEDGE],
                {
                    "mode": HIERARCHICAL_BY_KNOWLEDGE,
                    "count": len(result),
                    "section_ids": [r["section_id"] for r in result],
                },
                op_id,
            )
            return result
        except Exception as exc:
            self._emit(
                EventType.ERROR,
                {"op": "retrieval", "mode": HIERARCHICAL_BY_KNOWLEDGE, "error": str(exc)},
                op_id,
            )
            raise

    async def retrieve_by_file(
        self,
        knowledge_name: str,
        user_query: str,
        file_name_list: list[str],
        top_k: int = 5,
        max_per_read: int = 50,
        anchor_count: int = 4,
        op_id: str | None = None,
    ) -> list[dict]:
        op_id = op_id or uuid4().hex
        self._emit(
            RETRIEVAL_STARTED_FOR_MODE[HIERARCHICAL_BY_FILE],
            {"mode": HIERARCHICAL_BY_FILE, "query": user_query, "top_k": top_k, "knowledge": knowledge_name},
            op_id,
        )
        try:
            knowledge = self._kb.get(knowledge_name)
            candidates: list[dict] = []
            for file_name in file_name_list:
                for s in knowledge.list_sections(file_name):
                    s["knowledge_name"] = knowledge_name
                    candidates.append(s)
            selected = await self._scored_select(
                user_query, candidates, top_k, HIERARCHICAL_BY_FILE, op_id, self._section_prompt, SectionScoreOutput,
                id_field="section_id", max_per_read=max_per_read, anchor_count=anchor_count,
            )
            result = self._trim_output(selected)
            self._emit(
                RETRIEVAL_COMPLETED_FOR_MODE[HIERARCHICAL_BY_FILE],
                {
                    "mode": HIERARCHICAL_BY_FILE,
                    "count": len(result),
                    "section_ids": [r["section_id"] for r in result],
                    "knowledge": knowledge_name,
                },
                op_id,
            )
            return result
        except Exception as exc:
            self._emit(
                EventType.ERROR,
                {"op": "retrieval", "mode": HIERARCHICAL_BY_FILE, "error": str(exc), "knowledge": knowledge_name},
                op_id,
            )
            raise


def _file_id_of(section_id: str) -> str:
    return section_id.rsplit("-", 1)[0]


class AgreementBasedRetrievalPipeline(RetrievalPipeline):
    """Runs embedded + hierarchical, compares the section lists (v1 semantics).

    - exact list equality  -> Strong       -> fused top-k
    - positional file_id match (pos by pos) -> Weak -> both tagged lists
    - otherwise            -> Disagreement -> both tagged lists
    Return: {"agreement_type", "retrieved_content"}.
    """

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        embedded_pipeline: EmbeddedRetrievalPipeline,
        hierarchical_pipeline: HierarchicalRetrievalPipeline,
        bus: EventBus,
    ) -> None:
        super().__init__(knowledge_base, bus)
        self._embedded = embedded_pipeline
        self._hierarchical = hierarchical_pipeline

    @staticmethod
    def check_agreement(embedded_results: list[dict], hierarchical_results: list[dict]) -> str:
        if embedded_results == hierarchical_results:
            return "Strong Agreement"
        for e_result, h_result in zip(embedded_results, hierarchical_results):
            e_file_id = _file_id_of(e_result.get("section_id") or "")
            h_file_id = _file_id_of(h_result.get("section_id") or "")
            if e_file_id != h_file_id:
                return "Disagreement"
        return "Weak Agreement"

    def _result(
        self,
        agreement: str,
        embedded_results: list[dict],
        hierarchical_results: list[dict],
        top_k: int,
    ) -> dict:
        if agreement == "Strong Agreement":
            return {
                "agreement_type": agreement,
                "retrieved_content": embedded_results[:top_k],
            }
        return {
            "agreement_type": agreement,
            "retrieved_content": {
                "embedded_retrieval": embedded_results[:top_k],
                "hierarchical_retrieval": hierarchical_results[:top_k],
            },
        }

    async def retrieve_local_context(
        self,
        knowledge_name: str,
        user_query: str,
        top_k: int = 3,
        embedded_top_k: int = 4,
        hierarchical_top_k: int = 4,
        op_id: str | None = None,
    ) -> dict:
        op_id = op_id or uuid4().hex
        self._emit(
            RETRIEVAL_STARTED_FOR_MODE[LOCAL_AGREEMENT_RETRIEVAL],
            {"mode": LOCAL_AGREEMENT_RETRIEVAL, "query": user_query, "top_k": top_k, "knowledge": knowledge_name},
            op_id,
        )
        try:
            embedded_results = await self._embedded.retrieve_local_context(
                knowledge_name=knowledge_name, user_query=user_query, top_k=embedded_top_k
            )
            hierarchical_results = await self._hierarchical.retrieve_local_context(
                knowledge_name=knowledge_name, user_query=user_query, top_k=hierarchical_top_k
            )
            agreement = self.check_agreement(embedded_results, hierarchical_results)
            result = self._result(agreement, embedded_results, hierarchical_results, top_k)
            self._emit(
                RETRIEVAL_COMPLETED_FOR_MODE[LOCAL_AGREEMENT_RETRIEVAL],
                {"mode": LOCAL_AGREEMENT_RETRIEVAL, "agreement_type": agreement, "count": len(embedded_results)},
                op_id,
            )
            return result
        except Exception as exc:
            self._emit(
                EventType.ERROR,
                {"op": "retrieval", "mode": LOCAL_AGREEMENT_RETRIEVAL, "error": str(exc), "knowledge": knowledge_name},
                op_id,
            )
            raise

    async def retrieve_global_context(
        self,
        user_query: str,
        top_k: int = 3,
        embedded_top_k: int = 4,
        hierarchical_top_k: int = 4,
        top_k_knowledge: int = 2,
        full_retrieval: bool = False,
        op_id: str | None = None,
    ) -> dict:
        op_id = op_id or uuid4().hex
        self._emit(
            RETRIEVAL_STARTED_FOR_MODE[GLOBAL_AGREEMENT_RETRIEVAL],
            {"mode": GLOBAL_AGREEMENT_RETRIEVAL, "query": user_query, "top_k": top_k},
            op_id,
        )
        try:
            embedded_results = await self._embedded.retrieve_global_context(
                user_query=user_query, top_k=embedded_top_k
            )
            hierarchical_results = await self._hierarchical.retrieve_global_context(
                user_query=user_query,
                top_k_section=hierarchical_top_k,
                top_k_knowledge=top_k_knowledge,
                full_retrieval=full_retrieval,
            )
            agreement = self.check_agreement(embedded_results, hierarchical_results)
            result = self._result(agreement, embedded_results, hierarchical_results, top_k)
            self._emit(
                RETRIEVAL_COMPLETED_FOR_MODE[GLOBAL_AGREEMENT_RETRIEVAL],
                {"mode": GLOBAL_AGREEMENT_RETRIEVAL, "agreement_type": agreement, "count": len(embedded_results)},
                op_id,
            )
            return result
        except Exception as exc:
            self._emit(
                EventType.ERROR,
                {"op": "retrieval", "mode": GLOBAL_AGREEMENT_RETRIEVAL, "error": str(exc)},
                op_id,
            )
            raise


class VectorConditionedRetrievalPipeline(RetrievalPipeline):
    """Embedding-narrow then LLM-final (v1 semantics).

    - local: embedded top-k -> unique file_names -> hierarchical by_file.
    - global: embedded top-k -> unique knowledge_names -> hierarchical by_knowledge.
    """

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        embedded_pipeline: EmbeddedRetrievalPipeline,
        hierarchical_pipeline: HierarchicalRetrievalPipeline,
        bus: EventBus,
    ) -> None:
        super().__init__(knowledge_base, bus)
        self._embedded = embedded_pipeline
        self._hierarchical = hierarchical_pipeline

    async def retrieve_local_context(
        self,
        knowledge_name: str,
        user_query: str,
        top_k: int = 5,
        embedded_top_k: int = 5,
        op_id: str | None = None,
    ) -> list[dict]:
        op_id = op_id or uuid4().hex
        self._emit(
            RETRIEVAL_STARTED_FOR_MODE[LOCAL_VECTOR_CONDITIONED_RETRIEVAL],
            {
                "mode": LOCAL_VECTOR_CONDITIONED_RETRIEVAL,
                "query": user_query,
                "top_k": top_k,
                "knowledge": knowledge_name,
            },
            op_id,
        )
        try:
            embedded_results = await self._embedded.retrieve_local_context(
                knowledge_name=knowledge_name, user_query=user_query, top_k=embedded_top_k
            )
            file_name_list = list(dict.fromkeys(r["file_name"] for r in embedded_results))  # unique, order-preserving
            result = await self._hierarchical.retrieve_by_file(
                knowledge_name=knowledge_name,
                user_query=user_query,
                file_name_list=file_name_list,
                top_k=top_k,
            )
            self._emit(
                RETRIEVAL_COMPLETED_FOR_MODE[LOCAL_VECTOR_CONDITIONED_RETRIEVAL],
                {
                    "mode": LOCAL_VECTOR_CONDITIONED_RETRIEVAL,
                    "count": len(result),
                    "section_ids": [r["section_id"] for r in result],
                    "knowledge": knowledge_name,
                },
                op_id,
            )
            return result
        except Exception as exc:
            self._emit(
                EventType.ERROR,
                {"op": "retrieval", "mode": LOCAL_VECTOR_CONDITIONED_RETRIEVAL, "error": str(exc), "knowledge": knowledge_name},
                op_id,
            )
            raise

    async def retrieve_global_context(
        self,
        user_query: str,
        top_k_section: int = 5,
        embedded_top_k: int = 10,
        op_id: str | None = None,
    ) -> list[dict]:
        op_id = op_id or uuid4().hex
        self._emit(
            RETRIEVAL_STARTED_FOR_MODE[GLOBAL_VECTOR_CONDITIONED_RETRIEVAL],
            {"mode": GLOBAL_VECTOR_CONDITIONED_RETRIEVAL, "query": user_query, "top_k": top_k_section},
            op_id,
        )
        try:
            embedded_results = await self._embedded.retrieve_global_context(
                user_query=user_query, top_k=embedded_top_k
            )
            knowledge_name_list = list(dict.fromkeys(r["knowledge_name"] for r in embedded_results))
            result = await self._hierarchical.retrieve_by_knowledge(
                user_query=user_query,
                knowledge_name_list=knowledge_name_list,
                top_k_section=top_k_section,
            )
            self._emit(
                RETRIEVAL_COMPLETED_FOR_MODE[GLOBAL_VECTOR_CONDITIONED_RETRIEVAL],
                {
                    "mode": GLOBAL_VECTOR_CONDITIONED_RETRIEVAL,
                    "count": len(result),
                    "section_ids": [r["section_id"] for r in result],
                },
                op_id,
            )
            return result
        except Exception as exc:
            self._emit(
                EventType.ERROR,
                {"op": "retrieval", "mode": GLOBAL_VECTOR_CONDITIONED_RETRIEVAL, "error": str(exc)},
                op_id,
            )
            raise