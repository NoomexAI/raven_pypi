"""Application pipelines for Raven knowledge processing."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from llama_index.core.prompts.base import ChatPromptTemplate
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..core.errors import ErrorCode, RavenError, error_payload
from ..core.config import DEFAULT_MAX_DOCUMENT_PAGES, DEFAULT_MAX_SOURCE_FILE_BYTES
from ..core.events import Event, EventType
from ..core.operations import (
    Operation,
    OperationManager,
    OperationTask,
    OperationTaskRecord,
    OperationType,
)
from ..data_management.knowledge_base import KnowledgeBase
from ..document_processing.document_parser import DocumentParser
from ..document_processing.semantic_splitter import ProvenanceAwareSemanticSplitter

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




class IngestionRetryInput(BaseModel):
    """Durable inputs required to safely repeat one ingestion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    knowledge_name: str
    source_path: str
    source_sha256: str | None
    file_id: str
    breakpoint_percentile_threshold: int
    buffer_size: int
    max_extraction_retries: int
    chunk_size: int
    chunk_overlap: int
    max_source_size_bytes: int
    max_document_pages: int


class IngestionPipeline:
    """Section a source file, extract metadata, and store its sections."""

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        operation_manager: OperationManager,
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
        self._operation_manager = operation_manager
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
        retry_of: OperationTaskRecord | None = None,
        chunk_size: int = 512,
        chunk_overlap: int = 50,
        max_source_size_bytes: int = DEFAULT_MAX_SOURCE_FILE_BYTES,
        max_document_pages: int = DEFAULT_MAX_DOCUMENT_PAGES,
    ) -> OperationTask:
        if retry_of is None:
            path = Path(source_path).expanduser().resolve()
            retry_input = IngestionRetryInput(
                knowledge_name=knowledge_name,
                source_path=str(path),
                source_sha256=await asyncio.to_thread(
                    self._source_sha256_if_available,
                    path,
                ),
                file_id=uuid4().hex[:12],
                breakpoint_percentile_threshold=self._breakpoint_percentile_threshold,
                buffer_size=self._buffer_size,
                max_extraction_retries=self._max_extraction_retries,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                max_source_size_bytes=max_source_size_bytes,
                max_document_pages=max_document_pages,
            )
        else:
            try:
                retry_input = IngestionRetryInput.model_validate(retry_of.retry_input)
            except ValidationError as exc:
                raise RavenError(
                    ErrorCode.INVALID_RETRY_INPUT,
                    "The ingestion retry input is invalid.",
                ) from exc

        active_operation = operation or await self._operation_manager.create(
            OperationType.INGESTION_RUN
        )
        return await active_operation.run(
            OperationType.INGESTION_RUN,
            lambda active_operation: self._run(
                retry_input.knowledge_name,
                retry_input.source_path,
                llm=llm,
                embed_model=embed_model,
                operation=active_operation,
                file_id=retry_input.file_id,
                expected_source_sha256=retry_input.source_sha256,
                reconcile_before_run=retry_of is not None,
                chunk_size=retry_input.chunk_size,
                chunk_overlap=retry_input.chunk_overlap,
                max_source_size_bytes=retry_input.max_source_size_bytes,
                max_document_pages=retry_input.max_document_pages,
            ),
            retry_input=retry_input.model_dump(mode="json"),
            retry_of=retry_of,
        )


    async def _run(
        self,
        knowledge_name: str,
        source_path: str | Path,
        *,
        llm: Any,
        embed_model: Any,
        operation: Operation,
        file_id: str,
        expected_source_sha256: str | None,
        reconcile_before_run: bool,
        chunk_size: int = 512,
        chunk_overlap: int = 50,
        max_source_size_bytes: int = DEFAULT_MAX_SOURCE_FILE_BYTES,
        max_document_pages: int = DEFAULT_MAX_DOCUMENT_PAGES,
    ) -> dict[str, Any]:
        """Run the complete ingestion workflow in the caller-owned operation."""
        path = Path(source_path)
        file_name = path.name
        await self._emit(
            operation,
            EventType.INGESTION_STARTED,
            {
                "knowledge": knowledge_name,
                "file": file_name,
                "file_id": file_id,
            },
        )

        snapshot_path: Path | None = None
        ingestion_claimed = False
        try:
            knowledge = self._knowledge_base.get(knowledge_name)
            if reconcile_before_run:
                cleanup_task = await knowledge.reconcile_ingestion(
                    file_id,
                    operation=operation,
                )
                cleanup = await cleanup_task.result()
                if cleanup["status"] == "committed":
                    result = {
                        **cleanup["ingestion"],
                        "source_path": str(path),
                        "already_committed": True,
                    }
                    await self._emit(operation, EventType.INGESTION_COMPLETED, result)
                    return result

            if not await asyncio.to_thread(path.is_file):
                raise RavenError(
                    ErrorCode.SOURCE_FILE_NOT_FOUND,
                    f"Source file '{path}' was not found.",
                )

            source_size = await asyncio.to_thread(lambda: path.stat().st_size)
            if source_size > max_source_size_bytes:
                raise RavenError(
                    ErrorCode.SOURCE_FILE_TOO_LARGE,
                    f"Source file '{file_name}' exceeds the configured size limit.",
                    details={
                        "size_bytes": source_size,
                        "max_size_bytes": max_source_size_bytes,
                    },
                )

            await knowledge.claim_ingestion(file_name, file_id)
            ingestion_claimed = True
            snapshot_path = await asyncio.to_thread(
                self._snapshot_source,
                path,
                self._knowledge_base.paths.uploads_dir,
                file_id,
            )

            current_source_sha256 = await asyncio.to_thread(
                self._source_sha256,
                snapshot_path,
            )
            if (
                expected_source_sha256 is not None
                and current_source_sha256 != expected_source_sha256
            ):
                raise RavenError(
                    ErrorCode.SOURCE_FILE_CHANGED,
                    f"Source file '{path}' changed after ingestion was submitted.",
                    details={
                        "expected_sha256": expected_source_sha256,
                        "actual_sha256": current_source_sha256,
                    },
                )

            parsed_document = await self._document_parser.parse(
                snapshot_path,
                file_id=file_id,
                max_file_size_bytes=max_source_size_bytes,
                max_pages=max_document_pages,
            )
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
            ingest_task = await knowledge.ingest(
                file_name,
                sections,
                file_id=file_id,
                embed_model=embed_model,
                navigation_type=parsed_document.navigation_type.value,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                operation=operation,
            )
            result = await ingest_task.result()
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
        finally:
            if snapshot_path is not None:
                await asyncio.to_thread(snapshot_path.unlink, missing_ok=True)
            if ingestion_claimed:
                await knowledge.release_ingestion(file_name, file_id)


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


    @staticmethod
    def _source_sha256_if_available(path: Path) -> str | None:
        if not path.is_file():
            return None
        try:
            return IngestionPipeline._source_sha256(path)
        except OSError:
            return None


    @staticmethod
    def _source_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()


    @staticmethod
    def _snapshot_source(path: Path, directory: Path, file_id: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        snapshot = directory / f"{file_id}{path.suffix.lower()}"
        shutil.copyfile(path, snapshot)
        return snapshot
