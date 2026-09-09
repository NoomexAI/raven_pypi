"""Provenance-aware semantic splitting for parsed Raven documents."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from typing import Any

from llama_index.core.node_parser.text.utils import split_by_sentence_tokenizer
from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import ErrorCode, RavenError
from .document_parser import ElementType, NavigationType, ParsedDocument


class SemanticUnit(BaseModel):
    """One text unit evaluated by the semantic splitter."""

    model_config = ConfigDict(extra="forbid")

    text: str
    element_id: str
    element_order: int
    source_range: list[int] | None = Field(default=None, min_length=2, max_length=2)


class SemanticSection(BaseModel):
    """A semantic section with provenance ready for ingestion."""

    model_config = ConfigDict(extra="forbid")

    raw_content: str
    source_element_ids: list[str] = Field(default_factory=list)
    navigation_type: NavigationType
    source_range: list[int] | None = Field(default=None, min_length=2, max_length=2)


class ProvenanceAwareSemanticSplitter:
    """Split parsed elements while preserving their source provenance."""

    def __init__(
        self,
        embed_model: Any,
        *,
        breakpoint_percentile_threshold: int = 95,
        buffer_size: int = 1,
        sentence_splitter: Callable[[str], list[str]] | None = None,
    ) -> None:
        if not 0 < breakpoint_percentile_threshold <= 100:
            raise ValueError(
                "breakpoint_percentile_threshold must be between 1 and 100"
            )
        if buffer_size <= 0:
            raise ValueError("buffer_size must be positive")

        self._embed_model = embed_model
        self._breakpoint_percentile_threshold = breakpoint_percentile_threshold
        self._buffer_size = buffer_size
        self._sentence_splitter = sentence_splitter or split_by_sentence_tokenizer()


    async def split(self, document: ParsedDocument) -> list[SemanticSection]:
        """Return semantic sections with aggregated source metadata."""
        units = self._build_units(document)
        if not units:
            return []

        sentence_windows = self._build_sentence_windows(units)
        embeddings = await self._embed_model.aget_text_embedding_batch(
            [window for window in sentence_windows],
            show_progress=False,
        )
        self._validate_embeddings(embeddings, expected_count=len(sentence_windows))
        distances = await asyncio.to_thread(self._calculate_distances, embeddings)
        groups = await asyncio.to_thread(self._build_groups, units, distances)

        return [
            self._build_section(document, group)
            for group in groups
            if any(unit.text.strip() for unit in group)
        ]


    def _build_units(self, document: ParsedDocument) -> list[SemanticUnit]:
        units: list[SemanticUnit] = []

        for element_order, element in enumerate(document.elements):
            if not element.text.strip():
                continue

            if element.element_type in {
                ElementType.TEXT,
                ElementType.HEADING,
                ElementType.LIST_ITEM,
            }:
                texts = self._sentence_splitter(element.text)
            else:
                texts = [element.text]

            units.extend(
                SemanticUnit(
                    text=text,
                    element_id=element.element_id,
                    element_order=element_order,
                    source_range=element.source_range,
                )
                for text in texts
                if text.strip()
            )

        return units


    def _build_sentence_windows(self, units: Sequence[SemanticUnit]) -> list[str]:
        windows: list[str] = []

        for index in range(len(units)):
            start = max(0, index - self._buffer_size)
            end = min(len(units), index + self._buffer_size + 1)
            windows.append(self._join_units(units[start:end]))

        return windows


    def _calculate_distances(self, embeddings: Sequence[Sequence[float]]) -> list[float]:
        return [
            1 - self._embed_model.similarity(embeddings[index], embeddings[index + 1])
            for index in range(len(embeddings) - 1)
        ]


    @staticmethod
    def _validate_embeddings(
        embeddings: Sequence[Sequence[float]],
        *,
        expected_count: int,
    ) -> None:
        if len(embeddings) != expected_count:
            raise RavenError(
                ErrorCode.INVALID_EMBEDDING_RESULT,
                "Embedding model returned an invalid number of semantic vectors.",
            )
        dimensions = {len(vector) for vector in embeddings}
        if not dimensions or 0 in dimensions or len(dimensions) != 1:
            raise RavenError(
                ErrorCode.INVALID_EMBEDDING_RESULT,
                "Semantic vectors must have one consistent non-zero dimension.",
            )
        if any(
            not math.isfinite(float(value))
            for vector in embeddings
            for value in vector
        ):
            raise RavenError(
                ErrorCode.INVALID_EMBEDDING_RESULT,
                "Semantic vectors must contain only finite numeric values.",
            )


    def _build_groups(
        self,
        units: Sequence[SemanticUnit],
        distances: Sequence[float],
    ) -> list[list[SemanticUnit]]:
        if not distances:
            return [list(units)]

        threshold = self._percentile(
            distances,
            self._breakpoint_percentile_threshold,
        )
        breakpoints = [
            index for index, distance in enumerate(distances) if distance > threshold
        ]

        groups: list[list[SemanticUnit]] = []
        start_index = 0
        for breakpoint in breakpoints:
            groups.append(list(units[start_index : breakpoint + 1]))
            start_index = breakpoint + 1

        if start_index < len(units):
            groups.append(list(units[start_index:]))

        return groups


    def _build_section(
        self,
        document: ParsedDocument,
        units: Sequence[SemanticUnit],
    ) -> SemanticSection:
        first_order = min(unit.element_order for unit in units)
        last_order = max(unit.element_order for unit in units)
        source_elements = document.elements[first_order : last_order + 1]

        source_element_ids = list(dict.fromkeys(
            element.element_id for element in source_elements
        ))
        source_ranges = [
            element.source_range
            for element in source_elements
            if element.source_range is not None
        ]

        return SemanticSection(
            raw_content=self._join_units(units).strip(),
            source_element_ids=source_element_ids,
            navigation_type=document.navigation_type,
            source_range=self._merge_ranges(source_ranges),
        )


    @staticmethod
    def _join_units(units: Sequence[SemanticUnit]) -> str:
        if not units:
            return ""

        parts = [units[0].text]
        for previous, current in zip(units, units[1:]):
            separator = "" if previous.element_order == current.element_order else "\n\n"
            parts.append(f"{separator}{current.text}")
        return "".join(parts)


    @staticmethod
    def _merge_ranges(ranges: Sequence[list[int] | None]) -> list[int] | None:
        values = [value for value in ranges if value is not None]
        if not values:
            return None
        return [min(value[0] for value in values), max(value[1] for value in values)]


    @staticmethod
    def _percentile(values: Sequence[float], percentile: int) -> float:
        ordered = sorted(values)
        position = (len(ordered) - 1) * percentile / 100
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * weight
