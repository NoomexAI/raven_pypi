"""Application pipelines for Raven knowledge processing."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from llama_index.core.prompts.base import ChatPromptTemplate
from pydantic import BaseModel, ConfigDict, Field

from .document_parser import DocumentParser
from .errors import ErrorCode, RavenError, error_payload
from .events import Event, EventType
from .knowledge_base import KnowledgeBase
from .operations import Operation
from .semantic_splitter import ProvenanceAwareSemanticSplitter

logger = logging.getLogger(__name__)


METADATA_SYSTEM_PROMPT = """\
You are a precise data extraction agent. You are given ONE section of a document
and must produce its metadata as strict JSON.

Fields:
- summary: a concise summary of the section's main concept.
- keywords: relevant tags or identifiers found in this section.
- conditions: conditional states, thresholds, or functional constraints.
- definitions: short answers to the what/who questions answerable from this section.

Return only a valid JSON object matching the requested schema.
"""


class SectionMetadata(BaseModel):
    """Structured metadata extracted from one document section."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="Concise summary of the section's main concept")
    keywords: list[str] = Field(
        default_factory=list,
        description="Relevant tags and identifiers found in this section",
    )
    conditions: list[str] = Field(
        default_factory=list,
        description="Conditional states, thresholds, or functional constraints",
    )
    definitions: list[str] = Field(
        default_factory=list,
        description="Answers to what/who questions answerable from this section",
    )


class IngestionPipeline:
    """Section a source file, extract metadata, and store its sections."""

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        *,
        breakpoint_percentile_threshold: int = 95,
        buffer_size: int = 1,
        max_extraction_retries: int = 3,
        document_parser: DocumentParser | None = None,
    ) -> None:
        if not 0 < breakpoint_percentile_threshold <= 100:
            raise ValueError("breakpoint_percentile_threshold must be between 1 and 100")
        if buffer_size <= 0:
            raise ValueError("buffer_size must be positive")
        if max_extraction_retries <= 0:
            raise ValueError("max_extraction_retries must be positive")

        self._knowledge_base = knowledge_base
        self._breakpoint_percentile_threshold = breakpoint_percentile_threshold
        self._buffer_size = buffer_size
        self._max_extraction_retries = max_extraction_retries
        self._document_parser = document_parser or DocumentParser()
        self._prompt = ChatPromptTemplate.from_messages(
            [
                ("system", METADATA_SYSTEM_PROMPT),
                ("user", "SECTION TEXT:\n{section_text}"),
            ]
        )


    async def run(
        self,
        knowledge_name: str,
        source_path: str | Path,
        *,
        llm: Any,
        embed_model: Any,
        operation: Operation | None = None,
        chunk_size: int = 512,
        chunk_overlap: int = 50,
    ) -> dict[str, Any]:
        """Run the complete ingestion workflow in the caller-owned operation."""
        path = Path(source_path)
        file_name = path.name
        await self._emit(
            operation,
            EventType.INGESTION_STARTED,
            {"knowledge": knowledge_name, "file": file_name},
        )

        try:
            if not await asyncio.to_thread(path.is_file):
                raise RavenError(
                    ErrorCode.SOURCE_FILE_NOT_FOUND,
                    f"Source file '{path}' was not found.",
                )

            knowledge = self._knowledge_base.get(knowledge_name)
            file_id = uuid4().hex[:12]
            parsed_document = await self._document_parser.parse(path, file_id=file_id)
            self._raise_if_cancelled(operation)

            splitter = ProvenanceAwareSemanticSplitter(
                embed_model=embed_model,
                breakpoint_percentile_threshold=self._breakpoint_percentile_threshold,
                buffer_size=self._buffer_size,
            )
            semantic_sections = await splitter.split(parsed_document)
            total = len(semantic_sections)
            await self._emit(
                operation,
                EventType.INGESTION_PROGRESS,
                {
                    "knowledge": knowledge_name,
                    "file": file_name,
                    "stage": "sectioning",
                    "completed": total,
                    "total": total,
                },
            )

            if total == 0:
                raise RavenError(
                    ErrorCode.NO_SECTIONS_PRODUCED,
                    f"No sections were produced for file '{file_name}'.",
                )

            sections: list[dict[str, Any]] = []
            for section_index, semantic_section in enumerate(semantic_sections, start=1):
                self._raise_if_cancelled(operation)
                await self._emit(
                    operation,
                    EventType.INGESTION_PROGRESS,
                    {
                        "knowledge": knowledge_name,
                        "file": file_name,
                        "stage": "metadata_extraction",
                        "completed": section_index - 1,
                        "total": total,
                        "section_index": section_index,
                    },
                )
                section_text = semantic_section.raw_content
                metadata = await self._extract_metadata(
                    llm,
                    section_text,
                    section_index=section_index,
                    operation=operation,
                )
                sections.append(
                    {
                        "summary": metadata.summary,
                        "keywords": metadata.keywords,
                        "conditions": metadata.conditions,
                        "definitions": metadata.definitions,
                        "raw_content": section_text,
                        "source_element_ids": semantic_section.source_element_ids,
                        "source_range": semantic_section.source_range,
                    }
                )

            self._raise_if_cancelled(operation)
            await self._emit(
                operation,
                EventType.INGESTION_PROGRESS,
                {
                    "knowledge": knowledge_name,
                    "file": file_name,
                    "stage": "storage",
                    "completed": len(sections),
                    "total": total,
                },
            )
            result = await knowledge.ingest(
                file_name,
                sections,
                file_id=file_id,
                embed_model=embed_model,
                navigation_type=parsed_document.navigation_type.value,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                operation=operation,
            )
            result = {
                **result,
                "source_path": str(path),
                "section_count": len(sections),
            }
            await self._emit(operation, EventType.INGESTION_COMPLETED, result)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                operation,
                EventType.INGESTION_FAILED,
                {
                    "knowledge": knowledge_name,
                    "file": file_name,
                    "error": error_payload(exc),
                },
            )
            logger.exception("Ingestion failed for %s", path)
            raise


    async def _extract_metadata(
        self,
        llm: Any,
        section_text: str,
        *,
        section_index: int,
        operation: Operation | None,
    ) -> SectionMetadata:
        last_error: BaseException | None = None
        for attempt in range(1, self._max_extraction_retries + 1):
            self._raise_if_cancelled(operation)
            try:
                result = await llm.astructured_predict(
                    output_cls=SectionMetadata,
                    prompt=self._prompt,
                    section_text=section_text,
                )
                return SectionMetadata.model_validate(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Metadata extraction failed for section %s, attempt %s/%s",
                    section_index,
                    attempt,
                    self._max_extraction_retries,
                )

        raise RavenError(
            ErrorCode.SECTION_METADATA_EXTRACTION_FAILED,
            f"Metadata extraction failed for section {section_index}.",
            details={
                "section_index": section_index,
                "attempts": self._max_extraction_retries,
            },
        ) from last_error


    @staticmethod
    def _raise_if_cancelled(operation: Operation | None) -> None:
        if operation is not None:
            operation.raise_if_cancelled()


    @staticmethod
    async def _emit(
        operation: Operation | None,
        event_type: EventType,
        data: dict[str, Any],
    ) -> None:
        if operation is not None:
            await operation.publish(Event(type=event_type, data=data))
