"""Retrieval strategies for Raven knowledge databases."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from llama_index.core.prompts.base import ChatPromptTemplate
from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import ErrorCode, RavenError, error_payload
from ..core.events import Event, EventType
from ..core.operations import Operation, OperationManager, OperationTask, OperationType
from ..core.retrieval_scope import RetrievalScope
from ..data_management.knowledge_base import Knowledge, KnowledgeBase


class RetrievalPipeline:
    """Shared data access and event helpers for retrieval strategies."""

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        operation_manager: OperationManager,
    ) -> None:
        self._knowledge_base = knowledge_base
        self._operation_manager = operation_manager


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

    model_config = ConfigDict(extra="forbid")

    section_id: str
    score: int = Field(ge=0, le=10)


class SectionScoreOutput(BaseModel):
    """Structured LLM output for a section scoring batch."""

    model_config = ConfigDict(extra="forbid")

    scores: list[ScoredSection]


class ScoredKnowledge(BaseModel):
    """One LLM relevance score for a knowledge summary."""

    model_config = ConfigDict(extra="forbid")

    knowledge_name: str
    score: int = Field(ge=0, le=10)


class KnowledgeScoreOutput(BaseModel):
    """Structured LLM output for a knowledge scoring batch."""

    model_config = ConfigDict(extra="forbid")

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

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        embed_model: Any,
        operation_manager: OperationManager,
    ) -> None:
        super().__init__(knowledge_base, operation_manager)
        self._embed_model = embed_model


    async def _embed_query(
        self,
        user_query: str,
        knowledges: list[Knowledge],
        operation: Operation,
    ) -> list[float]:
        for knowledge in knowledges:
            self._raise_if_cancelled(operation)
            await knowledge.validate_embedding_model(self._embed_model)
        self._raise_if_cancelled(operation)
        return await self._embed_model.aget_query_embedding(user_query)


    async def _shortlist_files(
        self,
        user_query: str,
        resolved_file_ids: dict[str, frozenset[str]],
        top_k_files: int,
        *,
        operation: Operation,
    ) -> list[tuple[str, str]]:
        if top_k_files <= 0:
            return []
        targets = [
            (self._knowledge_base.get(name), file_ids)
            for name, file_ids in resolved_file_ids.items()
            if file_ids
        ]
        if not targets:
            return []

        query_vector = await self._embed_query(user_query, [knowledge for knowledge, _ in targets], operation)
        ranked_files: list[tuple[str, str, float]] = []
        for knowledge, file_ids in targets:
            self._raise_if_cancelled(operation)
            hits = await knowledge.search_files(query_vector, file_ids=file_ids, top_k=top_k_files)
            ranked_files.extend((knowledge.name, file_id, score) for file_id, score in hits)

        ranked_files.sort(key=lambda hit: hit[2], reverse=True)
        return [(name, file_id) for name, file_id, _score in ranked_files[:top_k_files]]


    async def retrieve(
        self,
        user_query: str,
        *,
        scope: RetrievalScope,
        top_k: int = 3,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Retrieve sections from the documents selected by scope."""
        if not isinstance(scope, RetrievalScope):
            raise TypeError("scope must be a RetrievalScope")
        active_operation = operation or await self._operation_manager.create(
            OperationType.RETRIEVAL_EMBEDDED
        )
        return await active_operation.run(
            OperationType.RETRIEVAL_EMBEDDED,
            lambda active_operation: self._retrieve_across_knowledges(
                user_query,
                top_k,
                scope=scope,
                operation=active_operation,
            ),
        )


    async def _retrieve_across_knowledges(
        self,
        user_query: str,
        top_k: int = 3,
        *,
        scope: RetrievalScope,
        resolved_file_ids: dict[str, frozenset[str]] | None = None,
        operation: Operation,
    ) -> list[dict[str, str]]:
        """Search eligible knowledges and rank their combined vector hits."""
        if top_k <= 0:
            return []

        event_scope = scope.model_dump(mode="json")
        await self._emit(
            operation,
            EventType.RETRIEVAL_EMBEDDED_STARTED,
            {
                "scope": event_scope,
                "query": user_query,
                "top_k": top_k,
            },
        )

        try:
            if resolved_file_ids is None:
                resolved_file_ids = await self._knowledge_base.resolve_scope(scope)
            knowledges = [
                (name, self._knowledge_base.get(name), file_ids)
                for name, file_ids in resolved_file_ids.items()
                if file_ids
            ]
            ranked_hits: list[dict[str, Any]] = []
            if knowledges:
                query_vector = await self._embed_query(
                    user_query, [knowledge for _, knowledge, _ in knowledges], operation
                )
                for knowledge_name, knowledge, file_ids in knowledges:
                    self._raise_if_cancelled(operation)
                    hits = await knowledge.search(
                        query_vector,
                        top_k=max(top_k * 4, top_k),
                        file_ids=file_ids,
                    )
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

                section = await asyncio.to_thread(
                    self._knowledge_base.get(knowledge_name).get_section,
                    section_id,
                )
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
                    "scope": event_scope,
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
                    "scope": event_scope,
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
        operation_manager: OperationManager,
        *,
        max_retries: int = MAX_SCORING_RETRIES,
        max_scoring_characters: int = 64_000,
    ) -> None:
        super().__init__(knowledge_base, operation_manager)
        if max_retries <= 0:
            raise ValueError("max_retries must be positive")
        if max_scoring_characters <= 0:
            raise ValueError("max_scoring_characters must be positive")

        self._llm = llm
        self._max_retries = max_retries
        self._max_scoring_characters = max_scoring_characters
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


    async def retrieve(
        self,
        user_query: str,
        *,
        scope: RetrievalScope,
        top_k_section: int = 3,
        top_k_knowledge: int = 2,
        full_retrieval: bool = False,
        max_per_read: int = 50,
        anchor_count: int = 4,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Score sections from the documents selected by scope."""
        if not isinstance(scope, RetrievalScope):
            raise TypeError("scope must be a RetrievalScope")
        active_operation = operation or await self._operation_manager.create(
            OperationType.RETRIEVAL_HIERARCHICAL
        )
        return await active_operation.run(
            OperationType.RETRIEVAL_HIERARCHICAL,
            lambda active_operation: self._retrieve_across_knowledges(
                user_query,
                top_k_section,
                scope=scope,
                top_k_knowledge=top_k_knowledge,
                full_retrieval=full_retrieval,
                max_per_read=max_per_read,
                anchor_count=anchor_count,
                operation=active_operation,
            ),
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

        batches = self._scoring_batches(
            candidates,
            id_field=id_field,
            max_per_read=max_per_read,
        )
        scores: dict[str, int] = {}
        order: list[str] = []

        for batch_index, batch in enumerate(batches, start=1):
            self._raise_if_cancelled(operation)
            batch_ids = {
                str(candidate[id_field])
                for candidate in batch
                if candidate.get(id_field) is not None
            }
            positions = {candidate_id: index for index, candidate_id in enumerate(order)}
            ranked = sorted(
                scores.items(),
                key=lambda item: (-item[1], positions[item[0]]),
            )
            all_by_id = {
                str(candidate[id_field]): candidate
                for candidate in candidates
                if candidate.get(id_field) is not None
            }
            anchors = json.dumps(
                [
                    {
                        **self._score_metadata(
                            all_by_id[candidate_id],
                            id_field=id_field,
                        ),
                        "score": score,
                    }
                    for candidate_id, score in ranked[:anchor_count]
                ],
                ensure_ascii=False,
            ) if ranked[:anchor_count] else "(none - first read)"

            metadata_batch = [
                self._bounded_score_metadata(candidate, id_field=id_field)
                for candidate in batch
            ]
            batch_scores: dict[str, int] | None = None
            last_error: Exception | None = None

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
                    batch_scores = self._validated_batch_scores(
                        result,
                        batch_ids=batch_ids,
                        id_field=id_field,
                        mode=mode,
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                    if attempt == self._max_retries:
                        raise

            if batch_scores is None:
                raise RuntimeError(
                    f"{mode}: scoring batch {batch_index} failed"
                ) from last_error

            for candidate in batch:
                candidate_id = str(candidate[id_field])
                scores[candidate_id] = batch_scores[candidate_id]
                order.append(candidate_id)

            await self._emit(
                operation,
                EventType.RETRIEVAL_HIERARCHICAL_READ,
                {
                    "mode": mode,
                    "batch": batch_index,
                    "batch_total": len(batches),
                    "candidates": len(batch),
                    "scored": len(batch_scores),
                },
            )

        positions = {candidate_id: index for index, candidate_id in enumerate(order)}
        ranked_ids = sorted(
            order,
            key=lambda candidate_id: (-scores[candidate_id], positions[candidate_id]),
        )
        by_id = {
            str(candidate[id_field]): candidate
            for candidate in candidates
            if candidate.get(id_field) is not None
        }
        return [by_id[candidate_id] for candidate_id in ranked_ids[:top_k]]


    @staticmethod
    def _validated_batch_scores(
        result: SectionScoreOutput | KnowledgeScoreOutput,
        *,
        batch_ids: set[str],
        id_field: str,
        mode: str,
    ) -> dict[str, int]:
        batch_scores: dict[str, int] = {}
        for entry in result.scores:
            candidate_id = getattr(entry, id_field, None)
            if (
                not isinstance(candidate_id, str)
                or candidate_id not in batch_ids
                or candidate_id in batch_scores
            ):
                raise RavenError(
                    ErrorCode.RETRIEVAL_SCORING_INVALID,
                    f"{mode}: the scorer returned an unknown or duplicate identifier.",
                )

            batch_scores[candidate_id] = entry.score

        missing_ids = sorted(batch_ids.difference(batch_scores))
        if missing_ids:
            raise RavenError(
                ErrorCode.RETRIEVAL_SCORING_INVALID,
                f"{mode}: the scorer did not score every candidate.",
                details={"missing_ids": missing_ids},
            )

        return batch_scores


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


    def _bounded_score_metadata(
        self,
        candidate: dict[str, Any],
        *,
        id_field: str,
    ) -> dict[str, Any]:
        metadata = self._score_metadata(candidate, id_field=id_field)
        per_candidate = max(1_000, self._max_scoring_characters // 4)
        for key, value in tuple(metadata.items()):
            if isinstance(value, str) and len(value) > per_candidate:
                metadata[key] = value[:per_candidate]
            elif isinstance(value, list):
                metadata[key] = [str(item)[:500] for item in value[:50]]
        return metadata


    def _scoring_batches(
        self,
        candidates: list[dict[str, Any]],
        *,
        id_field: str,
        max_per_read: int,
    ) -> list[list[dict[str, Any]]]:
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_size = 0
        for candidate in candidates:
            metadata = self._bounded_score_metadata(candidate, id_field=id_field)
            candidate_size = len(json.dumps(metadata, ensure_ascii=False))
            if current and (
                len(current) >= max_per_read
                or current_size + candidate_size > self._max_scoring_characters
            ):
                batches.append(current)
                current = []
                current_size = 0
            current.append(candidate)
            current_size += candidate_size
        if current:
            batches.append(current)
        return batches


    @staticmethod
    def _sections_of(
        knowledge: Knowledge,
        knowledge_name: str,
        file_ids: frozenset[str],
    ) -> list[dict[str, Any]]:
        return [
            {**section, "knowledge_name": knowledge_name}
            for file_id in sorted(file_ids)
            for section in knowledge.list_sections_by_file_id(file_id)
        ]


    async def _score_sections(
        self,
        user_query: str,
        knowledge_names: list[str],
        top_k: int,
        *,
        operation: Operation | None,
        max_per_read: int,
        anchor_count: int,
        file_ids_by_knowledge: dict[str, frozenset[str]],
    ) -> list[dict[str, str]]:
        candidates: list[dict[str, Any]] = []
        for knowledge_name in knowledge_names:
            self._raise_if_cancelled(operation)
            knowledge = self._knowledge_base.get(knowledge_name)
            file_ids = file_ids_by_knowledge[knowledge_name]
            candidates.extend(
                await asyncio.to_thread(self._sections_of, knowledge, knowledge_name, file_ids)
            )

        selected = await self._scored_select(
            user_query,
            candidates,
            top_k,
            operation=operation,
            prompt=self._section_prompt,
            output_cls=SectionScoreOutput,
            id_field="section_id",
            mode="scoped_section",
            max_per_read=max_per_read,
            anchor_count=anchor_count,
        )
        return self._trim_output(selected)


    async def _retrieve_across_knowledges(
        self,
        user_query: str,
        top_k_section: int = 3,
        *,
        scope: RetrievalScope,
        resolved_file_ids: dict[str, frozenset[str]] | None = None,
        top_k_knowledge: int = 2,
        full_retrieval: bool = False,
        max_per_read: int = 50,
        anchor_count: int = 4,
        operation: Operation,
    ) -> list[dict[str, str]]:
        """Optionally shortlist knowledges, then rank their eligible sections."""
        event_scope = scope.model_dump(mode="json")
        await self._emit(
            operation,
            EventType.RETRIEVAL_HIERARCHICAL_STARTED,
            {
                "scope": event_scope,
                "query": user_query,
                "top_k": top_k_section,
            },
        )

        try:
            self._raise_if_cancelled(operation)
            if resolved_file_ids is None:
                resolved_file_ids = await self._knowledge_base.resolve_scope(scope)
            knowledge_names = [name for name, file_ids in resolved_file_ids.items() if file_ids]
            if not full_retrieval and not scope.selections and len(knowledge_names) > 1:
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
                    mode="scoped_knowledge",
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
                max_per_read=max_per_read,
                anchor_count=anchor_count,
                file_ids_by_knowledge=resolved_file_ids,
            )
            await self._emit(
                operation,
                EventType.RETRIEVAL_HIERARCHICAL_COMPLETED,
                {
                    "scope": event_scope,
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
                    "scope": event_scope,
                    "error": error_payload(exc),
                },
            )
            raise


def _file_id_of(section_id: str) -> str:
    """Return the file ID portion of Raven's file-section ID."""
    return section_id.rsplit("-", 1)[0]


class AgreementBasedRetrievalPipeline(RetrievalPipeline):
    """Compare the ranked evidence from embedded and hierarchical retrieval."""

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        embedded_pipeline: EmbeddedRetrievalPipeline,
        hierarchical_pipeline: HierarchicalRetrievalPipeline,
        operation_manager: OperationManager,
    ) -> None:
        super().__init__(knowledge_base, operation_manager)
        self._embedded = embedded_pipeline
        self._hierarchical = hierarchical_pipeline


    async def retrieve(
        self,
        user_query: str,
        *,
        scope: RetrievalScope,
        top_k: int = 3,
        top_k_knowledge: int = 2,
        full_retrieval: bool = False,
        max_per_read: int = 50,
        anchor_count: int = 4,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Compare two ranked lists drawn from one document scope."""
        if not isinstance(scope, RetrievalScope):
            raise TypeError("scope must be a RetrievalScope")
        active_operation = operation or await self._operation_manager.create(
            OperationType.RETRIEVAL_AGREEMENT
        )
        return await active_operation.run(
            OperationType.RETRIEVAL_AGREEMENT,
            lambda active_operation: self._retrieve_across_knowledges(
                user_query,
                top_k,
                scope=scope,
                top_k_knowledge=top_k_knowledge,
                full_retrieval=full_retrieval,
                max_per_read=max_per_read,
                anchor_count=anchor_count,
                operation=active_operation,
            ),
        )


    @staticmethod
    def check_agreement(
        embedded_results: list[dict[str, str]],
        hierarchical_results: list[dict[str, str]],
    ) -> str:
        """Compare section order for strong agreement and file overlap for weak agreement."""
        embedded_sections = [(item["knowledge_name"], item["section_id"]) for item in embedded_results]
        hierarchical_sections = [(item["knowledge_name"], item["section_id"]) for item in hierarchical_results]
        if embedded_sections and embedded_sections == hierarchical_sections:
            return "Strong Agreement"

        embedded_files = {(knowledge, _file_id_of(section_id)) for knowledge, section_id in embedded_sections}
        hierarchical_files = {(knowledge, _file_id_of(section_id)) for knowledge, section_id in hierarchical_sections}
        if embedded_files.isdisjoint(hierarchical_files):
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


    async def _retrieve_across_knowledges(
        self,
        user_query: str,
        top_k: int = 3,
        *,
        scope: RetrievalScope,
        top_k_knowledge: int = 2,
        full_retrieval: bool = False,
        max_per_read: int = 50,
        anchor_count: int = 4,
        operation: Operation,
    ) -> dict[str, Any]:
        """Compare embedded and hierarchical retrieval across eligible knowledges."""
        event_scope = scope.model_dump(mode="json")
        await self._emit(
            operation,
            EventType.RETRIEVAL_AGREEMENT_STARTED,
            {
                "scope": event_scope,
                "query": user_query,
                "top_k": top_k,
            },
        )

        try:
            self._raise_if_cancelled(operation)
            resolved_file_ids = await self._knowledge_base.resolve_scope(scope)
            embedded_task = await operation.run(
                OperationType.RETRIEVAL_EMBEDDED,
                lambda active_operation: self._embedded._retrieve_across_knowledges(
                    user_query,
                    top_k,
                    scope=scope,
                    resolved_file_ids=resolved_file_ids,
                    operation=active_operation,
                ),
            )
            embedded_results = await embedded_task.result()
            self._raise_if_cancelled(operation)
            hierarchical_task = await operation.run(
                OperationType.RETRIEVAL_HIERARCHICAL,
                lambda active_operation: self._hierarchical._retrieve_across_knowledges(
                    user_query,
                    top_k,
                    scope=scope,
                    resolved_file_ids=resolved_file_ids,
                    top_k_knowledge=top_k_knowledge,
                    full_retrieval=full_retrieval,
                    max_per_read=max_per_read,
                    anchor_count=anchor_count,
                    operation=active_operation,
                ),
            )
            hierarchical_results = await hierarchical_task.result()
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
                    "scope": event_scope,
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
                    "scope": event_scope,
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
        operation_manager: OperationManager,
    ) -> None:
        super().__init__(knowledge_base, operation_manager)
        self._embedded = embedded_pipeline
        self._hierarchical = hierarchical_pipeline


    async def retrieve(
        self,
        user_query: str,
        *,
        scope: RetrievalScope,
        top_k_files: int = 5,
        top_k_sections: int = 5,
        max_per_read: int = 50,
        anchor_count: int = 4,
        operation: Operation | None = None,
    ) -> OperationTask:
        """Shortlist files by vector similarity, then score sections within them."""
        if not isinstance(scope, RetrievalScope):
            raise TypeError("scope must be a RetrievalScope")
        active_operation = operation or await self._operation_manager.create(
            OperationType.RETRIEVAL_VECTOR_CONDITIONED
        )
        return await active_operation.run(
            OperationType.RETRIEVAL_VECTOR_CONDITIONED,
            lambda active_operation: self._retrieve_scoped(
                user_query,
                scope=scope,
                top_k_files=top_k_files,
                top_k_sections=top_k_sections,
                max_per_read=max_per_read,
                anchor_count=anchor_count,
                operation=active_operation,
            ),
        )


    async def _retrieve_scoped(
        self,
        user_query: str,
        *,
        scope: RetrievalScope,
        top_k_files: int,
        top_k_sections: int,
        max_per_read: int,
        anchor_count: int,
        operation: Operation,
    ) -> list[dict[str, str]]:
        event_scope = scope.model_dump(mode="json")
        await self._emit(
            operation,
            EventType.RETRIEVAL_VECTOR_CONDITIONED_STARTED,
            {
                "scope": event_scope,
                "query": user_query,
                "top_k_files": top_k_files,
                "top_k_sections": top_k_sections,
            },
        )

        try:
            self._raise_if_cancelled(operation)
            resolved_file_ids = await self._knowledge_base.resolve_scope(scope)
            shortlist_task = await operation.run(
                OperationType.RETRIEVAL_EMBEDDED_FILES,
                lambda active_operation: self._embedded._shortlist_files(
                    user_query, resolved_file_ids, top_k_files, operation=active_operation
                ),
            )
            shortlist: list[tuple[str, str]] = await shortlist_task.result()

            result: list[dict[str, str]] = []
            if shortlist and top_k_sections > 0:
                shortlisted_files: dict[str, set[str]] = {}
                for knowledge_name, file_id in shortlist:
                    shortlisted_files.setdefault(knowledge_name, set()).add(file_id)
                file_ids_by_knowledge = {
                    name: frozenset(file_ids) for name, file_ids in shortlisted_files.items()
                }
                self._raise_if_cancelled(operation)
                hierarchical_task = await operation.run(
                    OperationType.RETRIEVAL_HIERARCHICAL,
                    lambda active_operation: self._hierarchical._retrieve_across_knowledges(
                        user_query,
                        top_k_sections,
                        scope=scope,
                        resolved_file_ids=file_ids_by_knowledge,
                        full_retrieval=True,
                        max_per_read=max_per_read,
                        anchor_count=anchor_count,
                        operation=active_operation,
                    ),
                )
                result = await hierarchical_task.result()

            await self._emit(
                operation,
                EventType.RETRIEVAL_VECTOR_CONDITIONED_COMPLETED,
                {
                    "scope": event_scope,
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
                {"scope": event_scope, "error": error_payload(exc)},
            )
            raise
