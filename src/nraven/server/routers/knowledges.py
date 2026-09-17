"""Knowledge CRUD, file navigation, and section evidence endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status

from ...core.errors import ErrorCode, RavenError
from ...core.upload_retention import (
    bind_upload_operation,
    create_upload_source,
    remove_upload_source,
)
from ...h_api.raven import Raven
from ..dependencies import get_runtime_registry, lease_raven, resolve_user_id, submit_task
from ..runtime import UserRuntimeRegistry
from ..schemas import (
    ErrorResponse,
    IngestPathRequest,
    KnowledgeCreateRequest,
    KnowledgeDetailsResponse,
    KnowledgeFilePageResponse,
    KnowledgeFileResponse,
    KnowledgePageResponse,
    KnowledgeSectionResponse,
    KnowledgeSectionsResponse,
    KnowledgeStatsResponse,
    KnowledgeSummaryResponse,
    KnowledgeUpdateRequest,
    OperationTaskReference,
)


router = APIRouter(prefix="/api/v1/knowledges", tags=["knowledges"])
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    414: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


@router.get(
    "",
    response_model=KnowledgePageResponse,
    responses=_ERROR_RESPONSES,
)
async def list_knowledges(
    raven: Annotated[Raven, Depends(lease_raven)],
    limit: Annotated[int | None, Query(ge=1)] = None,
    after_name: str | None = None,
) -> KnowledgePageResponse:
    page_size = _page_size(raven, limit)
    records = await raven.list_knowledges_page(
        limit=page_size + 1,
        after_name=after_name,
    )
    has_more = len(records) > page_size
    selected = records[:page_size]
    return KnowledgePageResponse(
        items=[KnowledgeSummaryResponse.model_validate(item) for item in selected],
        next_cursor=selected[-1]["name"] if has_more and selected else None,
    )


@router.post(
    "",
    response_model=KnowledgeDetailsResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
async def create_knowledge(
    request: KnowledgeCreateRequest,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> KnowledgeDetailsResponse:
    task = await raven.create_knowledge(request.name, request.user_summary)
    knowledge = await task.result()
    return KnowledgeDetailsResponse.model_validate(knowledge.meta)


@router.get(
    "/{name}",
    response_model=KnowledgeDetailsResponse,
    responses=_ERROR_RESPONSES,
)
async def get_knowledge(
    name: str,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> KnowledgeDetailsResponse:
    return KnowledgeDetailsResponse.model_validate(raven.get_knowledge_details(name))


@router.patch(
    "/{name}",
    response_model=KnowledgeDetailsResponse,
    responses=_ERROR_RESPONSES,
)
async def update_knowledge(
    name: str,
    request: KnowledgeUpdateRequest,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> KnowledgeDetailsResponse:
    task = await raven.update_knowledge(name, user_summary=request.user_summary)
    await task.result()
    return KnowledgeDetailsResponse.model_validate(raven.get_knowledge_details(name))


@router.delete(
    "/{name}",
    response_model=OperationTaskReference,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def delete_knowledge(
    name: str,
    raven: Annotated[Raven, Depends(lease_raven)],
    request: Request,
) -> OperationTaskReference:
    raven.get_knowledge(name)
    task = await submit_task(
        request,
        raven,
        lambda: raven.delete_knowledge(name),
    )
    return OperationTaskReference.from_task(task)


@router.get(
    "/{name}/files",
    response_model=KnowledgeFilePageResponse,
    responses=_ERROR_RESPONSES,
)
async def list_files(
    name: str,
    raven: Annotated[Raven, Depends(lease_raven)],
    limit: Annotated[int | None, Query(ge=1)] = None,
    after_file_id: str | None = None,
) -> KnowledgeFilePageResponse:
    page_size = _page_size(raven, limit)
    records = await raven.list_knowledge_files_page(
        name,
        limit=page_size + 1,
        after_file_id=after_file_id,
    )
    has_more = len(records) > page_size
    selected = records[:page_size]
    return KnowledgeFilePageResponse(
        items=[KnowledgeFileResponse.model_validate(item) for item in selected],
        next_cursor=selected[-1]["file_id"] if has_more and selected else None,
    )


@router.post(
    "/{name}/files",
    response_model=OperationTaskReference,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def upload_file(
    name: str,
    file: Annotated[UploadFile, File()],
    raven: Annotated[Raven, Depends(lease_raven)],
    request: Request,
) -> OperationTaskReference:
    """Stage a bounded browser source and submit observable ingestion."""
    filename = _upload_filename(file.filename)
    raven.get_knowledge(name)
    source = await asyncio.to_thread(
        create_upload_source,
        raven.paths.uploads_dir,
        filename,
        raven.system_config.upload_retry_retention_seconds,
    )
    submitted = False
    try:
        async def stage_and_ingest():
            copy_task = asyncio.create_task(
                asyncio.to_thread(
                    _copy_upload,
                    file.file,
                    source,
                    raven.system_config.max_source_file_bytes,
                )
            )
            try:
                await asyncio.shield(copy_task)
            except asyncio.CancelledError:
                await asyncio.gather(copy_task, return_exceptions=True)
                raise
            return await raven.ingest(name, source)

        task = await submit_task(
            request,
            raven,
            stage_and_ingest,
        )
        submitted = True
        await asyncio.to_thread(
            bind_upload_operation,
            source,
            task.operation_id,
            task.task_id,
        )
        registry = get_runtime_registry(request)
        registry.track_upload(
            resolve_user_id(request),
            task,
            source,
            raven.paths.uploads_dir,
        )
        return OperationTaskReference.from_task(task)
    finally:
        await file.close()
        if not submitted:
            await asyncio.to_thread(
                remove_upload_source,
                source,
                raven.paths.uploads_dir,
            )


@router.post(
    "/{name}/ingest-path",
    response_model=OperationTaskReference,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def ingest_trusted_path(
    name: str,
    request: IngestPathRequest,
    raven: Annotated[Raven, Depends(lease_raven)],
    http_request: Request,
) -> OperationTaskReference:
    config = raven.system_config
    if not config.trusted_ingestion_enabled:
        raise RavenError(
            ErrorCode.TRUSTED_INGESTION_DISABLED,
            "Trusted local-path ingestion is disabled.",
        )
    try:
        source = await asyncio.to_thread(
            lambda: Path(request.source_path).expanduser().resolve(strict=True)
        )
    except (OSError, ValueError) as exc:
        raise RavenError(
            ErrorCode.SOURCE_FILE_NOT_FOUND,
            "The source path does not resolve to a file.",
        ) from exc
    if not any(source.is_relative_to(root) for root in config.allowed_ingestion_roots):
        raise RavenError(
            ErrorCode.INGESTION_PATH_NOT_ALLOWED,
            "The source path is outside the allowed ingestion roots.",
        )
    if not await asyncio.to_thread(source.is_file):
        raise RavenError(
            ErrorCode.SOURCE_FILE_NOT_FOUND,
            "The source path is not a regular file.",
        )
    raven.get_knowledge(name)
    task = await submit_task(
        http_request,
        raven,
        lambda: raven.ingest(name, source),
    )
    return OperationTaskReference.from_task(task)


@router.delete(
    "/{name}/files/{file_id}",
    response_model=OperationTaskReference,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def delete_file(
    name: str,
    file_id: str,
    raven: Annotated[Raven, Depends(lease_raven)],
    request: Request,
) -> OperationTaskReference:
    task = await submit_task(
        request,
        raven,
        lambda: raven.delete_knowledge_file(name, file_id),
    )
    return OperationTaskReference.from_task(task)


@router.get(
    "/{name}/files/{file_name}/sections",
    response_model=KnowledgeSectionsResponse,
    responses=_ERROR_RESPONSES,
)
async def list_sections(
    name: str,
    file_name: str,
    raven: Annotated[Raven, Depends(lease_raven)],
    limit: Annotated[int | None, Query(ge=1)] = None,
    after_section_index: Annotated[int, Query(ge=0)] = 0,
) -> KnowledgeSectionsResponse:
    page_size = _page_size(raven, limit)
    sections = await raven.list_file_sections_page(
        name,
        file_name,
        limit=page_size + 1,
        after_section_index=after_section_index,
    )
    has_more = len(sections) > page_size
    selected = sections[:page_size]
    return KnowledgeSectionsResponse(
        items=[KnowledgeSectionResponse.model_validate(item) for item in selected],
        next_cursor=(
            selected[-1]["section_index"] if has_more and selected else None
        ),
    )


@router.get(
    "/{name}/files/{file_name}/sections/{section_id}",
    response_model=KnowledgeSectionResponse,
    responses=_ERROR_RESPONSES,
)
async def get_section(
    name: str,
    file_name: str,
    section_id: str,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> KnowledgeSectionResponse:
    section = await raven.get_knowledge_section(
        name,
        section_id,
        file_name=file_name,
    )
    return KnowledgeSectionResponse.model_validate(section)


@router.get(
    "/{name}/stats",
    response_model=KnowledgeStatsResponse,
    responses=_ERROR_RESPONSES,
)
async def knowledge_stats(
    name: str,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> KnowledgeStatsResponse:
    return KnowledgeStatsResponse(
        vector_count=await raven.count_knowledge_vectors(name),
        file_count=await raven.count_knowledge_files(name),
    )


def _page_size(raven: Raven, requested: int | None) -> int:
    maximum = raven.system_config.operation_page_size
    if requested is None:
        return maximum
    if requested > maximum:
        raise RavenError(
            ErrorCode.INVALID_LIST_PAGE_SIZE,
            f"List page size cannot exceed {maximum}.",
            details={"maximum": maximum},
        )
    return requested


def _upload_filename(value: str | None) -> str:
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    if (
        not value
        or len(value) > 255
        or value.strip() != value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 or character in '<>:"|?*' for character in value)
        or value.split(".", 1)[0].upper() in reserved
        or Path(value).suffix.lower() not in {".txt", ".md", ".docx", ".pdf"}
    ):
        raise RavenError(
            ErrorCode.INVALID_UPLOAD_FILENAME,
            "Upload filenames must be simple .txt, .md, .docx, or .pdf names.",
        )
    return value


def _copy_upload(file: Any, destination: Path, maximum_bytes: int) -> None:
    total = 0
    with destination.open("xb") as output:
        while chunk := file.read(1024 * 1024):
            total += len(chunk)
            if total > maximum_bytes:
                raise RavenError(
                    ErrorCode.SOURCE_FILE_TOO_LARGE,
                    "The upload exceeds the configured source-file size limit.",
                    details={"max_size_bytes": maximum_bytes},
                )
            output.write(chunk)
