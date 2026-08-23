"""Retrieval strategies for Raven knowledge databases."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from llama_index.core.prompts.base import ChatPromptTemplate
from pydantic import BaseModel, Field

from ..core.errors import error_payload
from ..core.events import Event, EventType
from ..core.operations import Operation
from ..data_management.knowledge_base import KnowledgeBase


class RetrievalPipeline:
    """Shared data access and event helpers for retrieval strategies."""

    def __init__(self, knowledge_base: KnowledgeBase) -> None:
        self._knowledge_base = knowledge_base


    @staticmethod
    def _section_result(
        knowledge_name: str,
        section: dict[str, Any],
        section_id: str,
    ) -> dict[str, str]:
        return {
            "knowledge_name": knowledge_name,
            "file_name": str(section.get("file_name", "")),
            "section_id": section_id,
            "raw_content": str(section.get("raw_content", "")),
        }


    @staticmethod
    async def _emit(
        operation: Operation | None,
        event_type: EventType,
        data: dict[str, Any],
    ) -> None:
        if operation is not None:
            await operation.publish(Event(type=event_type, data=data))


    @staticmethod
    def _raise_if_cancelled(operation: Operation | None) -> None:
        if operation is not None:
            operation.raise_if_cancelled()


    async def _knowledge_names(self) -> list[str]:
        summaries = await self._knowledge_base.list()
        return [str(summary["safe_name"]) for summary in summaries]


    def _sections_of(self, knowledge_name: str) -> list[dict[str, Any]]:
        knowledge = self._knowledge_base.get(knowledge_name)
        sections: list[dict[str, Any]] = []
        for file_info in knowledge.list_files():
            for section in knowledge.list_sections(file_info["file_name"]):
                sections.append({**section, "knowledge_name": knowledge_name})
        return sections


    def _knowledge_summary(self, knowledge_name: str) -> str:
        return self._knowledge_base.get(knowledge_name).get_summary()


    @staticmethod
    def _trim_output(selected: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [
            {
                "knowledge_name": str(section["knowledge_name"]),
                "file_name": str(section["file_name"]),
                "section_id": str(section["section_id"]),
                "raw_content": str(section.get("raw_content", "")),
            }
            for section in selected
        ]


class ScoredSection(BaseModel):
    """One LLM relevance score for a section candidate."""

    section_id: str
    score: int = Field(ge=0, le=10)


class SectionScoreOutput(BaseModel):
    """Structured LLM output for a section scoring batch."""

    scores: list[ScoredSection]


class ScoredKnowledge(BaseModel):
    """One LLM relevance score for a knowledge summary."""

    knowledge_name: str
    score: int = Field(ge=0, le=10)


class KnowledgeScoreOutput(BaseModel):
    """Structured LLM output for a knowledge scoring batch."""

    scores: list[ScoredKnowledge]


MAX_SCORING_RETRIES = 3

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


class EmbeddedRetrievalPipeline(RetrievalPipeline):
    """Retrieve complete sections using vector similarity."""

    def __init__(self, knowledge_base: KnowledgeBase, embed_model: Any) -> None:
        super().__init__(knowledge_base)
        self._embed_model = embed_model


    async def retrieve_local_context(
        self,
        knowledge_name: str,
        user_query: str,
        top_k: int = 3,
        *,
        operation: Operation | None = None,
    ) -> list[dict[str, str]]:
        """Retrieve sections from one knowledge database."""
        if top_k <= 0:
            return []

        await self._emit(
            operation,
            EventType.RETRIEVAL_EMBEDDED_STARTED,
            {
                "scope": "local",
                "knowledge": knowledge_name,
                "query": user_query,
                "top_k": top_k,
            },
        )

        try:
            self._raise_if_cancelled(operation)
            query_vector = await self._embed_model.aget_text_embedding(user_query)
            self._raise_if_cancelled(operation)

            knowledge = self._knowledge_base.get(knowledge_name)
            hits = await knowledge.search(query_vector, top_k=top_k)
            results: list[dict[str, str]] = []
            seen: set[str] = set()

            for hit in hits:
                self._raise_if_cancelled(operation)
                section_id = hit.get("section_id")
                if not isinstance(section_id, str) or section_id in seen:
                    continue

                section = knowledge.get_section(section_id)
                if section is None:
                    continue

                seen.add(section_id)
                results.append(
                    self._section_result(knowledge_name, section, section_id)
                )

            await self._emit(
                operation,
                EventType.RETRIEVAL_EMBEDDED_COMPLETED,
                {
                    "scope": "local",
                    "knowledge": knowledge_name,
                    "count": len(results),
                    "section_ids": [result["section_id"] for result in results],
                },
            )
            return results
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                operation,
                EventType.RETRIEVAL_EMBEDDED_FAILED,
                {
                    "scope": "local",
                    "knowledge": knowledge_name,
                    "error": error_payload(exc),
                },
            )
            raise


    async def retrieve_global_context(
        self,
        user_query: str,
        top_k: int = 3,
        *,
        operation: Operation | None = None,
    ) -> list[dict[str, str]]:
        """Retrieve sections across all knowledge databases."""
        if top_k <= 0:
            return []

        await self._emit(
            operation,
            EventType.RETRIEVAL_EMBEDDED_STARTED,
            {
                "scope": "global",
                "query": user_query,
                "top_k": top_k,
            },
        )

        try:
            self._raise_if_cancelled(operation)
            query_vector = await self._embed_model.aget_text_embedding(user_query)
            self._raise_if_cancelled(operation)

            ranked_hits: list[dict[str, Any]] = []
            for knowledge_name in await self._knowledge_names():
                self._raise_if_cancelled(operation)
                knowledge = self._knowledge_base.get(knowledge_name)
                hits = await knowledge.search(query_vector, top_k=top_k)
                ranked_hits.extend(
                    {
                        "knowledge_name": knowledge_name,
                        **hit,
                    }
                    for hit in hits
                )

            ranked_hits.sort(
                key=lambda hit: float(hit.get("score", 0.0)),
                reverse=True,
            )

            results: list[dict[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for hit in ranked_hits:
                self._raise_if_cancelled(operation)
                knowledge_name = hit.get("knowledge_name")
                section_id = hit.get("section_id")
                if not isinstance(knowledge_name, str) or not isinstance(section_id, str):
                    continue

                key = (knowledge_name, section_id)
                if key in seen:
                    continue

                section = self._knowledge_base.get(knowledge_name).get_section(section_id)
                if section is None:
                    continue

                seen.add(key)
                results.append(
                    self._section_result(knowledge_name, section, section_id)
                )
                if len(results) >= top_k:
                    break

            await self._emit(
                operation,
                EventType.RETRIEVAL_EMBEDDED_COMPLETED,
                {
                    "scope": "global",
                    "count": len(results),
                    "section_ids": [result["section_id"] for result in results],
                },
            )
            return results
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                operation,
                EventType.RETRIEVAL_EMBEDDED_FAILED,
                {
                    "scope": "global",
                    "error": error_payload(exc),
                },
            )
            raise


class HierarchicalRetrievalPipeline(RetrievalPipeline):
    """Rank section metadata with an LLM and return the selected sections."""

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        llm: Any,
        *,
        max_retries: int = MAX_SCORING_RETRIES,
    ) -> None:
        super().__init__(knowledge_base)
        if max_retries <= 0:
            raise ValueError("max_retries must be positive")

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


    async def _scored_select(
        self,
        user_query: str,
        candidates: list[dict[str, Any]],
        top_k: int,
        *,
        operation: Operation | None,
        prompt: ChatPromptTemplate,
        output_cls: type[SectionScoreOutput] | type[KnowledgeScoreOutput],
        id_field: str,
        mode: str,
        max_per_read: int,
        anchor_count: int,
    ) -> list[dict[str, Any]]:
        if top_k <= 0 or not candidates:
            return []
        if max_per_read <= 0:
            raise ValueError("max_per_read must be positive")
        if anchor_count < 0:
            raise ValueError("anchor_count must be non-negative")

        batches = [
            candidates[index : index + max_per_read]
            for index in range(0, len(candidates), max_per_read)
        ]
        scores: dict[str, int] = {}
        order: list[str] = []

        for batch_index, batch in enumerate(batches, start=1):
            self._raise_if_cancelled(operation)
            batch_ids = {
                str(candidate[id_field])
                for candidate in batch
                if candidate.get(id_field) is not None
            }
            ranked = sorted(
                scores.items(),
                key=lambda item: (-item[1], order.index(item[0])),
            )
            anchors = "\n".join(
                f"{candidate_id}: score={score}/10"
                for candidate_id, score in ranked[:anchor_count]
            ) or "(none - first read)"

            metadata_batch = [
                self._score_metadata(candidate, id_field=id_field)
                for candidate in batch
            ]
            result: SectionScoreOutput | KnowledgeScoreOutput | None = None

            for attempt in range(1, self._max_retries + 1):
                self._raise_if_cancelled(operation)
                try:
                    raw_result = await self._llm.astructured_predict(
                        output_cls=output_cls,
                        prompt=prompt,
                        user_query=user_query,
                        anchors=anchors,
                        candidates=json.dumps(
                            metadata_batch,
                            ensure_ascii=False,
                        ),
                    )
                    result = output_cls.model_validate(raw_result)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if attempt == self._max_retries:
                        raise

            if result is None:
                raise RuntimeError(
                    f"{mode}: scoring batch {batch_index} failed"
                )

            scored_count = 0
            for entry in result.scores:
                candidate_id = getattr(entry, id_field, None)
                if (
                    not isinstance(candidate_id, str)
                    or candidate_id not in batch_ids
                    or candidate_id in scores
                ):
                    continue

                scores[candidate_id] = entry.score
                order.append(candidate_id)
                scored_count += 1

            await self._emit(
                operation,
                EventType.RETRIEVAL_HIERARCHICAL_READ,
                {
                    "mode": mode,
                    "batch": batch_index,
                    "batch_total": len(batches),
                    "candidates": len(batch),
                    "scored": scored_count,
                },
            )

        ranked_ids = sorted(
            order,
            key=lambda candidate_id: (-scores[candidate_id], order.index(candidate_id)),
        )
        by_id = {
            str(candidate[id_field]): candidate
            for candidate in candidates
            if candidate.get(id_field) is not None
        }
        return [by_id[candidate_id] for candidate_id in ranked_ids[:top_k]]


    @staticmethod
    def _score_metadata(
        candidate: dict[str, Any],
        *,
        id_field: str,
    ) -> dict[str, Any]:
        if id_field == "section_id":
            return {
                "section_id": candidate.get("section_id"),
                "file_name": candidate.get("file_name", ""),
                "summary": candidate.get("summary", ""),
                "keywords": candidate.get("keywords", []),
                "conditions": candidate.get("conditions", []),
                "definitions": candidate.get("definitions", []),
            }
        return {
            "knowledge_name": candidate.get("knowledge_name", ""),
            "summary": candidate.get("summary", ""),
        }


    async def _score_sections(
        self,
        user_query: str,
        knowledge_names: list[str],
        top_k: int,
        *,
        operation: Operation | None,
        mode: str,
        max_per_read: int,
        anchor_count: int,
    ) -> list[dict[str, str]]:
        candidates: list[dict[str, Any]] = []
        for knowledge_name in knowledge_names:
            candidates.extend(self._sections_of(knowledge_name))

        selected = await self._scored_select(
            user_query,
            candidates,
            top_k,
            operation=operation,
            prompt=self._section_prompt,
            output_cls=SectionScoreOutput,
            id_field="section_id",
            mode=mode,
            max_per_read=max_per_read,
            anchor_count=anchor_count,
        )
        return self._trim_output(selected)


    async def retrieve_local_context(
        self,
        knowledge_name: str,
        user_query: str,
        top_k: int = 3,
        *,
        max_per_read: int = 50,
        anchor_count: int = 4,
        operation: Operation | None = None,
    ) -> list[dict[str, str]]:
        """Rank sections from one knowledge using metadata."""
        await self._emit(
            operation,
            EventType.RETRIEVAL_HIERARCHICAL_STARTED,
            {
                "scope": "local",
                "knowledge": knowledge_name,
                "query": user_query,
                "top_k": top_k,
            },
        )

        try:
            self._raise_if_cancelled(operation)
            result = await self._score_sections(
                user_query,
                [knowledge_name],
                top_k,
                operation=operation,
                mode="local",
                max_per_read=max_per_read,
                anchor_count=anchor_count,
            )
            await self._emit(
                operation,
                EventType.RETRIEVAL_HIERARCHICAL_COMPLETED,
                {
                    "scope": "local",
                    "knowledge": knowledge_name,
                    "count": len(result),
                    "section_ids": [item["section_id"] for item in result],
                },
            )
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                operation,
                EventType.RETRIEVAL_HIERARCHICAL_FAILED,
                {
                    "scope": "local",
                    "knowledge": knowledge_name,
                    "error": error_payload(exc),
                },
            )
            raise


    async def retrieve_global_context(
        self,
        user_query: str,
        top_k_section: int = 3,
        *,
        top_k_knowledge: int = 2,
        full_retrieval: bool = False,
        max_per_read: int = 50,
        anchor_count: int = 4,
        operation: Operation | None = None,
    ) -> list[dict[str, str]]:
        """Rank knowledge summaries, then rank sections within selected knowledges."""
        await self._emit(
            operation,
            EventType.RETRIEVAL_HIERARCHICAL_STARTED,
            {
                "scope": "global",
                "query": user_query,
                "top_k": top_k_section,
            },
        )

        try:
            self._raise_if_cancelled(operation)
            knowledge_names = await self._knowledge_names()
            if not full_retrieval:
                knowledge_candidates = [
                    {
                        "knowledge_name": knowledge_name,
                        "summary": self._knowledge_summary(knowledge_name) or "(no summary)",
                    }
                    for knowledge_name in knowledge_names
                ]
                selected = await self._scored_select(
                    user_query,
                    knowledge_candidates,
                    top_k_knowledge,
                    operation=operation,
                    prompt=self._knowledge_prompt,
                    output_cls=KnowledgeScoreOutput,
                    id_field="knowledge_name",
                    mode="global_knowledge",
                    max_per_read=max_per_read,
                    anchor_count=anchor_count,
                )
                knowledge_names = [
                    str(candidate["knowledge_name"])
                    for candidate in selected
                ]

            result = await self._score_sections(
                user_query,
                knowledge_names,
                top_k_section,
                operation=operation,
                mode="global_section",
                max_per_read=max_per_read,
                anchor_count=anchor_count,
            )
            await self._emit(
                operation,
                EventType.RETRIEVAL_HIERARCHICAL_COMPLETED,
                {
                    "scope": "global",
                    "count": len(result),
                    "section_ids": [item["section_id"] for item in result],
                },
            )
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                operation,
                EventType.RETRIEVAL_HIERARCHICAL_FAILED,
                {
                    "scope": "global",
                    "error": error_payload(exc),
                },
            )
            raise


    async def retrieve_by_knowledge(
        self,
        user_query: str,
        knowledge_names: list[str],
        top_k_section: int = 5,
        *,
        max_per_read: int = 50,
        anchor_count: int = 4,
        operation: Operation | None = None,
    ) -> list[dict[str, str]]:
        """Rank sections within an explicit knowledge subset."""
        self._raise_if_cancelled(operation)
        result = await self._score_sections(
            user_query,
            knowledge_names,
            top_k_section,
            operation=operation,
            mode="by_knowledge",
            max_per_read=max_per_read,
            anchor_count=anchor_count,
        )
        return result


    async def retrieve_by_file(
        self,
        knowledge_name: str,
        user_query: str,
        file_names: list[str],
        top_k: int = 5,
        *,
        max_per_read: int = 50,
        anchor_count: int = 4,
        operation: Operation | None = None,
    ) -> list[dict[str, str]]:
        """Rank sections within an explicit file subset."""
        self._raise_if_cancelled(operation)
        knowledge = self._knowledge_base.get(knowledge_name)
        candidates: list[dict[str, Any]] = []
        for file_name in file_names:
            candidates.extend(
                {**section, "knowledge_name": knowledge_name}
                for section in knowledge.list_sections(file_name)
            )

        selected = await self._scored_select(
            user_query,
            candidates,
            top_k,
            operation=operation,
            prompt=self._section_prompt,
            output_cls=SectionScoreOutput,
            id_field="section_id",
            mode="by_file",
            max_per_read=max_per_read,
            anchor_count=anchor_count,
        )
        return self._trim_output(selected)


def _file_id_of(section_id: str) -> str:
    """Return the file ID portion of Raven's file-section ID."""
    return section_id.rsplit("-", 1)[0]


class AgreementBasedRetrievalPipeline(RetrievalPipeline):
    """Compare embedded and hierarchical retrieval using the v1 contract."""

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        embedded_pipeline: EmbeddedRetrievalPipeline,
        hierarchical_pipeline: HierarchicalRetrievalPipeline,
    ) -> None:
        super().__init__(knowledge_base)
        self._embedded = embedded_pipeline
        self._hierarchical = hierarchical_pipeline


    @staticmethod
    def check_agreement(
        embedded_results: list[dict[str, str]],
        hierarchical_results: list[dict[str, str]],
    ) -> str:
        """Classify results using the existing v1 comparison semantics."""
        if embedded_results == hierarchical_results:
            return "Strong Agreement"

        for embedded, hierarchical in zip(embedded_results, hierarchical_results):
            embedded_file_id = _file_id_of(embedded.get("section_id", ""))
            hierarchical_file_id = _file_id_of(
                hierarchical.get("section_id", "")
            )
            if embedded_file_id != hierarchical_file_id:
                return "Disagreement"

        return "Weak Agreement"


    @staticmethod
    def _build_result(
        agreement: str,
        embedded_results: list[dict[str, str]],
        hierarchical_results: list[dict[str, str]],
        top_k: int,
    ) -> dict[str, Any]:
        if agreement == "Strong Agreement":
            retrieved_content: Any = embedded_results[:top_k]
        else:
            retrieved_content = {
                "embedded_retrieval": embedded_results[:top_k],
                "hierarchical_retrieval": hierarchical_results[:top_k],
            }

        return {
            "agreement_type": agreement,
            "retrieved_content": retrieved_content,
        }


    async def retrieve_local_context(
        self,
        knowledge_name: str,
        user_query: str,
        top_k: int = 3,
        *,
        embedded_top_k: int = 4,
        hierarchical_top_k: int = 4,
        max_per_read: int = 50,
        anchor_count: int = 4,
        operation: Operation | None = None,
    ) -> dict[str, Any]:
        """Compare local embedded and hierarchical retrieval."""
        await self._emit(
            operation,
            EventType.RETRIEVAL_AGREEMENT_STARTED,
            {
                "scope": "local",
                "knowledge": knowledge_name,
                "query": user_query,
                "top_k": top_k,
            },
        )

        try:
            self._raise_if_cancelled(operation)
            embedded_results = await self._embedded.retrieve_local_context(
                knowledge_name,
                user_query,
                top_k=embedded_top_k,
                operation=operation,
            )
            self._raise_if_cancelled(operation)
            hierarchical_results = await self._hierarchical.retrieve_local_context(
                knowledge_name,
                user_query,
                top_k=hierarchical_top_k,
                max_per_read=max_per_read,
                anchor_count=anchor_count,
                operation=operation,
            )
            agreement = self.check_agreement(
                embedded_results,
                hierarchical_results,
            )
            result = self._build_result(
                agreement,
                embedded_results,
                hierarchical_results,
                top_k,
            )
            await self._emit(
                operation,
                EventType.RETRIEVAL_AGREEMENT_COMPLETED,
                {
                    "scope": "local",
                    "knowledge": knowledge_name,
                    "agreement_type": agreement,
                },
            )
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                operation,
                EventType.RETRIEVAL_AGREEMENT_FAILED,
                {
                    "scope": "local",
                    "knowledge": knowledge_name,
                    "error": error_payload(exc),
                },
            )
            raise


    async def retrieve_global_context(
        self,
        user_query: str,
        top_k: int = 3,
        *,
        embedded_top_k: int = 4,
        hierarchical_top_k: int = 4,
        top_k_knowledge: int = 2,
        full_retrieval: bool = False,
        max_per_read: int = 50,
        anchor_count: int = 4,
        operation: Operation | None = None,
    ) -> dict[str, Any]:
        """Compare global embedded and hierarchical retrieval."""
        await self._emit(
            operation,
            EventType.RETRIEVAL_AGREEMENT_STARTED,
            {
                "scope": "global",
                "query": user_query,
                "top_k": top_k,
            },
        )

        try:
            self._raise_if_cancelled(operation)
            embedded_results = await self._embedded.retrieve_global_context(
                user_query,
                top_k=embedded_top_k,
                operation=operation,
            )
            self._raise_if_cancelled(operation)
            hierarchical_results = await self._hierarchical.retrieve_global_context(
                user_query,
                top_k_section=hierarchical_top_k,
                top_k_knowledge=top_k_knowledge,
                full_retrieval=full_retrieval,
                max_per_read=max_per_read,
                anchor_count=anchor_count,
                operation=operation,
            )
            agreement = self.check_agreement(
                embedded_results,
                hierarchical_results,
            )
            result = self._build_result(
                agreement,
                embedded_results,
                hierarchical_results,
                top_k,
            )
            await self._emit(
                operation,
                EventType.RETRIEVAL_AGREEMENT_COMPLETED,
                {
                    "scope": "global",
                    "agreement_type": agreement,
                },
            )
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                operation,
                EventType.RETRIEVAL_AGREEMENT_FAILED,
                {
                    "scope": "global",
                    "error": error_payload(exc),
                },
            )
            raise


class VectorConditionedRetrievalPipeline(RetrievalPipeline):
    """Use vector retrieval to narrow the scope for hierarchical retrieval."""

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        embedded_pipeline: EmbeddedRetrievalPipeline,
        hierarchical_pipeline: HierarchicalRetrievalPipeline,
    ) -> None:
        super().__init__(knowledge_base)
        self._embedded = embedded_pipeline
        self._hierarchical = hierarchical_pipeline


    async def retrieve_local_context(
        self,
        knowledge_name: str,
        user_query: str,
        top_k: int = 5,
        *,
        embedded_top_k: int = 5,
        max_per_read: int = 50,
        anchor_count: int = 4,
        operation: Operation | None = None,
    ) -> list[dict[str, str]]:
        """Narrow local retrieval to vector-selected files before scoring sections."""
        await self._emit(
            operation,
            EventType.RETRIEVAL_VECTOR_CONDITIONED_STARTED,
            {
                "scope": "local",
                "knowledge": knowledge_name,
                "query": user_query,
                "top_k": top_k,
            },
        )

        try:
            self._raise_if_cancelled(operation)
            embedded_results = await self._embedded.retrieve_local_context(
                knowledge_name,
                user_query,
                top_k=embedded_top_k,
                operation=operation,
            )
            file_names = list(
                dict.fromkeys(result["file_name"] for result in embedded_results)
            )
            self._raise_if_cancelled(operation)
            result = await self._hierarchical.retrieve_by_file(
                knowledge_name,
                user_query,
                file_names,
                top_k=top_k,
                max_per_read=max_per_read,
                anchor_count=anchor_count,
                operation=operation,
            )
            await self._emit(
                operation,
                EventType.RETRIEVAL_VECTOR_CONDITIONED_COMPLETED,
                {
                    "scope": "local",
                    "knowledge": knowledge_name,
                    "count": len(result),
                    "section_ids": [item["section_id"] for item in result],
                },
            )
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                operation,
                EventType.RETRIEVAL_VECTOR_CONDITIONED_FAILED,
                {
                    "scope": "local",
                    "knowledge": knowledge_name,
                    "error": error_payload(exc),
                },
            )
            raise


    async def retrieve_global_context(
        self,
        user_query: str,
        top_k_section: int = 5,
        *,
        embedded_top_k: int = 10,
        max_per_read: int = 50,
        anchor_count: int = 4,
        operation: Operation | None = None,
    ) -> list[dict[str, str]]:
        """Narrow global retrieval to vector-selected knowledges before scoring sections."""
        await self._emit(
            operation,
            EventType.RETRIEVAL_VECTOR_CONDITIONED_STARTED,
            {
                "scope": "global",
                "query": user_query,
                "top_k": top_k_section,
            },
        )

        try:
            self._raise_if_cancelled(operation)
            embedded_results = await self._embedded.retrieve_global_context(
                user_query,
                top_k=embedded_top_k,
                operation=operation,
            )
            knowledge_names = list(
                dict.fromkeys(
                    result["knowledge_name"] for result in embedded_results
                )
            )
            self._raise_if_cancelled(operation)
            result = await self._hierarchical.retrieve_by_knowledge(
                user_query,
                knowledge_names,
                top_k_section=top_k_section,
                max_per_read=max_per_read,
                anchor_count=anchor_count,
                operation=operation,
            )
            await self._emit(
                operation,
                EventType.RETRIEVAL_VECTOR_CONDITIONED_COMPLETED,
                {
                    "scope": "global",
                    "count": len(result),
                    "section_ids": [item["section_id"] for item in result],
                },
            )
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                operation,
                EventType.RETRIEVAL_VECTOR_CONDITIONED_FAILED,
                {
                    "scope": "global",
                    "error": error_payload(exc),
                },
            )
            raise
