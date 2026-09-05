"""Reconstruct source-file views from Raven retrieval results."""

from __future__ import annotations

import asyncio
from typing import Any

from ..core.errors import ErrorCode, RavenError, error_payload
from ..core.events import Event, EventType
from ..core.operations import Operation, OperationManager, OperationTask, OperationType
from ..data_management.knowledge_base import KnowledgeBase


class Reconstructor:
    """Build highlighted, provenance-aware views of retrieved files."""

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        operation_manager: OperationManager,
    ) -> None:
        self._knowledge_base = knowledge_base
        self._operation_manager = operation_manager


    async def reconstruct(
        self,
        retrieval_result: list[dict[str, Any]] | dict[str, Any] | None,
        *,
        operation: Operation | None = None,
    ) -> OperationTask:
        active_operation = operation or await self._operation_manager.create(
            OperationType.RECONSTRUCTION_RECONSTRUCT
        )
        return await active_operation.run(
            OperationType.RECONSTRUCTION_RECONSTRUCT,
            lambda active_operation: self._reconstruct(
                retrieval_result,
                operation=active_operation,
            ),
        )


    async def _reconstruct(
        self,
        retrieval_result: list[dict[str, Any]] | dict[str, Any] | None,
        *,
        operation: Operation,
    ) -> list[dict[str, Any]]:
        """Return complete file views with retrieved sections highlighted."""
        await self._emit(
            operation,
            EventType.RECONSTRUCTION_STARTED,
            {},
        )

        try:
            highlighted_sections = self._normalize_retrieval_result(retrieval_result)
            self._raise_if_cancelled(operation)
            grouped_sections = self._group_sections(highlighted_sections)
            reconstructed_files: list[dict[str, Any]] = []

            for (knowledge_name, file_name), section_ids in grouped_sections.items():
                self._raise_if_cancelled(operation)
                reconstructed_file = self._reconstruct_file(
                    knowledge_name,
                    file_name,
                    section_ids,
                )
                reconstructed_files.append(reconstructed_file)
                await self._emit(
                    operation,
                    EventType.RECONSTRUCTION_FILE,
                    reconstructed_file,
                )

            await self._emit(
                operation,
                EventType.RECONSTRUCTION_COMPLETED,
                {
                    "file_count": len(reconstructed_files),
                    "section_count": sum(
                        len(file["sections"])
                        for file in reconstructed_files
                    ),
                },
            )
            return reconstructed_files
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                operation,
                EventType.RECONSTRUCTION_FAILED,
                {"error": error_payload(exc)},
            )
            raise


    def _reconstruct_file(
        self,
        knowledge_name: str,
        file_name: str,
        highlighted_ids: set[str],
    ) -> dict[str, Any]:
        knowledge = self._knowledge_base.get(knowledge_name)
        file_info = next(
            (
                file
                for file in knowledge.list_files()
                if file.get("file_name") == file_name
            ),
            None,
        )
        if file_info is None:
            raise RavenError(
                ErrorCode.FILE_NOT_FOUND,
                f"File '{file_name}' does not exist in knowledge '{knowledge_name}'.",
            )

        sections = [
            {
                "section_id": section["section_id"],
                "highlighted": section["section_id"] in highlighted_ids,
                "raw_content": section.get("raw_content", ""),
                "source_range": section.get("source_range"),
            }
            for section in knowledge.list_sections(file_name)
        ]
        return {
            "knowledge_name": knowledge_name,
            "file_name": file_name,
            "navigation_type": file_info.get("navigation_type", "none"),
            "sections": sections,
        }


    @classmethod
    def _normalize_retrieval_result(
        cls,
        retrieval_result: list[dict[str, Any]] | dict[str, Any] | None,
    ) -> list[dict[str, str]]:
        if retrieval_result is None:
            return []

        if isinstance(retrieval_result, list):
            return cls._deduplicate(retrieval_result)

        if not isinstance(retrieval_result, dict):
            return []

        retrieved_content = retrieval_result.get("retrieved_content")
        if isinstance(retrieved_content, list):
            return cls._deduplicate(retrieved_content)

        if not isinstance(retrieved_content, dict):
            return []

        combined = [
            *cls._as_section_list(retrieved_content.get("embedded_retrieval")),
            *cls._as_section_list(retrieved_content.get("hierarchical_retrieval")),
        ]
        return cls._deduplicate(combined)


    @staticmethod
    def _as_section_list(value: Any) -> list[dict[str, Any]]:
        return value if isinstance(value, list) else []


    @classmethod
    def _deduplicate(cls, sections: list[dict[str, Any]]) -> list[dict[str, str]]:
        unique: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()

        for section in sections:
            if not isinstance(section, dict):
                raise RavenError(
                    ErrorCode.INVALID_METADATA,
                    "Retrieval results must contain section objects.",
                )

            knowledge_name = section.get("knowledge_name")
            file_name = section.get("file_name")
            section_id = section.get("section_id")
            if not isinstance(knowledge_name, str) or not knowledge_name:
                raise RavenError(
                    ErrorCode.INVALID_METADATA,
                    "Retrieval sections must contain a valid knowledge_name.",
                )
            if not isinstance(file_name, str) or not file_name:
                raise RavenError(
                    ErrorCode.INVALID_METADATA,
                    "Retrieval sections must contain a valid file_name.",
                )
            if not isinstance(section_id, str) or not section_id:
                raise RavenError(
                    ErrorCode.INVALID_METADATA,
                    "Retrieval sections must contain a valid section_id.",
                )

            key = (knowledge_name, file_name, section_id)
            if key in seen:
                continue

            seen.add(key)
            unique.append(
                {
                    "knowledge_name": knowledge_name,
                    "file_name": file_name,
                    "section_id": section_id,
                }
            )

        return unique


    @staticmethod
    def _group_sections(
        sections: list[dict[str, str]],
    ) -> dict[tuple[str, str], set[str]]:
        grouped: dict[tuple[str, str], set[str]] = {}
        for section in sections:
            key = (section["knowledge_name"], section["file_name"])
            grouped.setdefault(key, set()).add(section["section_id"])
        return grouped


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
