"""Document parsing and source-provenance normalization for Raven."""

from __future__ import annotations

import asyncio
import mimetypes
from enum import StrEnum
from pathlib import Path
from typing import Any

from docling.document_converter import DocumentConverter
from pydantic import BaseModel, ConfigDict, Field

from .errors import ErrorCode, RavenError


class NavigationType(StrEnum):
    """Navigation systems currently supported by Raven."""

    NONE = "none"
    PAGE = "page"


class ElementType(StrEnum):
    """Normalized document element types exposed by the parser."""

    TEXT = "text"
    HEADING = "heading"
    LIST_ITEM = "list_item"
    TABLE = "table"
    PICTURE = "picture"
    CAPTION = "caption"
    FORMULA = "formula"
    UNKNOWN = "unknown"


class ParsedElement(BaseModel):
    """One ordered parser element with its source location."""

    model_config = ConfigDict(extra="forbid")

    element_id: str
    element_type: ElementType
    text: str = ""
    navigation_type: NavigationType = NavigationType.NONE
    source_range: list[int] | None = Field(default=None, min_length=2, max_length=2)


class ParsedDocument(BaseModel):
    """Normalized output of document parsing before semantic sectioning."""

    model_config = ConfigDict(extra="forbid")

    source_path: str
    file_name: str
    media_type: str | None = None
    navigation_type: NavigationType = NavigationType.NONE
    elements: list[ParsedElement] = Field(default_factory=list)

    def text_projection(self) -> str:
        """Return ordered text used as input for later semantic splitting."""
        return "\n\n".join(
            element.text.strip()
            for element in self.elements
            if element.text.strip()
        )


class DocumentParser:
    """Convert supported files into normalized, provenance-aware elements."""

    SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".pdf", ".docx"})

    def __init__(self, converter: DocumentConverter | None = None) -> None:
        self._converter = converter


    async def parse(
        self,
        source_path: str | Path,
        *,
        file_id: str,
    ) -> ParsedDocument:
        """Parse a source file without blocking the event loop."""
        self._validate_file_id(file_id)
        return await asyncio.to_thread(self._parse_sync, Path(source_path), file_id)


    def _parse_sync(self, source_path: Path, file_id: str) -> ParsedDocument:
        path = source_path.expanduser().resolve()
        self._validate_source(path)

        try:
            if path.suffix.lower() in {".txt", ".md", ".markdown"}:
                return self._parse_plain_text(path, file_id)
            return self._parse_with_docling(path, file_id)
        except RavenError:
            raise
        except (OSError, UnicodeError) as exc:
            raise RavenError(
                ErrorCode.SOURCE_FILE_UNREADABLE,
                f"Source file '{path}' could not be read.",
            ) from exc
        except Exception as exc:
            raise RavenError(
                ErrorCode.DOCUMENT_PARSE_FAILED,
                f"Source file '{path}' could not be parsed.",
            ) from exc


    @classmethod
    def _validate_source(cls, path: Path) -> None:
        if not path.is_file():
            raise RavenError(
                ErrorCode.SOURCE_FILE_NOT_FOUND,
                f"Source file '{path}' was not found.",
            )
        if path.suffix.lower() not in cls.SUPPORTED_SUFFIXES:
            raise RavenError(
                ErrorCode.UNSUPPORTED_SOURCE_FILE,
                f"File type '{path.suffix or '<none>'}' is not supported.",
                details={"supported_extensions": sorted(cls.SUPPORTED_SUFFIXES)},
            )


    @staticmethod
    def _validate_file_id(file_id: str) -> None:
        if not isinstance(file_id, str) or not file_id.strip():
            raise ValueError("file_id must be a non-empty string")


    @staticmethod
    def _parse_plain_text(path: Path, file_id: str) -> ParsedDocument:
        text = path.read_text(encoding="utf-8")
        element_type = ElementType.TEXT
        element = ParsedElement(
            element_id=DocumentParser._element_id(file_id, 1),
            element_type=element_type,
            text=text,
        )
        return ParsedDocument(
            source_path=str(path),
            file_name=path.name,
            media_type=mimetypes.guess_type(path.name)[0],
            elements=[element],
        )


    def _parse_with_docling(self, path: Path, file_id: str) -> ParsedDocument:
        converter = self._converter or DocumentConverter()
        self._converter = converter
        result = converter.convert(path)
        document = result.document

        elements = [
            self._normalize_item(item, document, file_id, index)
            for index, (item, _level) in enumerate(document.iterate_items(), start=1)
        ]
        navigation_type = (
            NavigationType.PAGE
            if any(element.source_range is not None for element in elements)
            else NavigationType.NONE
        )
        elements = [
            element.model_copy(update={"navigation_type": navigation_type})
            for element in elements
        ]

        return ParsedDocument(
            source_path=str(path),
            file_name=path.name,
            media_type=mimetypes.guess_type(path.name)[0],
            navigation_type=navigation_type,
            elements=elements,
        )


    @classmethod
    def _normalize_item(
        cls,
        item: Any,
        document: Any,
        file_id: str,
        index: int,
    ) -> ParsedElement:
        label = cls._label_value(item)
        element_type = cls._element_type(item, label)
        text = cls._element_text(item, document, element_type)

        return ParsedElement(
            element_id=cls._element_id(file_id, index),
            element_type=element_type,
            text=text,
            source_range=cls._page_range(item),
        )


    @staticmethod
    def _element_id(file_id: str, index: int) -> str:
        return f"{file_id}-element-{index}"


    @staticmethod
    def _label_value(item: Any) -> str:
        label = getattr(item, "label", "")
        return str(getattr(label, "value", label)).lower()


    @staticmethod
    def _element_type(item: Any, label: str) -> ElementType:
        item_name = type(item).__name__.lower()
        if label == "section_header" or "header" in item_name:
            return ElementType.HEADING
        if label == "table" or "table" in item_name:
            return ElementType.TABLE
        if label == "picture" or "picture" in item_name:
            return ElementType.PICTURE
        if label == "caption" or "caption" in item_name:
            return ElementType.CAPTION
        if label == "formula" or "formula" in item_name:
            return ElementType.FORMULA
        if label == "list_item" or "listitem" in item_name:
            return ElementType.LIST_ITEM
        if label == "text" or "text" in item_name:
            return ElementType.TEXT
        return ElementType.UNKNOWN


    @staticmethod
    def _element_text(item: Any, document: Any, element_type: ElementType) -> str:
        if element_type is ElementType.TABLE:
            export_to_markdown = getattr(item, "export_to_markdown", None)
            if callable(export_to_markdown):
                return str(export_to_markdown(doc=document) or "")

        text = getattr(item, "text", "")
        return text if isinstance(text, str) else ""


    @staticmethod
    def _page_range(item: Any) -> list[int] | None:
        page_numbers = {
            page_no
            for provenance in (getattr(item, "prov", None) or [])
            if (page_no := getattr(provenance, "page_no", None)) is not None
        }
        if not page_numbers:
            return None
        return [min(page_numbers), max(page_numbers)]
