"""Recorded manual actions and activity history for a conversation."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from ...core.config import SQLITE_MAX_INTEGER
from ...core.operations import OperationTask
from ...h_api.raven import Raven
from ...session.session import Session
from ..dependencies import lease_raven, submit_task
from ..schemas import (
    ErrorResponse,
    ManualActivityPageResponse,
    ManualActivityRemovalResponse,
    ManualRetrievalRequest,
    OperationTaskReference,
    SourceReconstructionRequest,
)


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
router = APIRouter(
    prefix="/api/v1/conversations/{conversation_id}/activities",
    tags=["conversation activities"],
    responses=_ERROR_RESPONSES,
)
RavenLease = Annotated[Raven, Depends(lease_raven)]


@router.get("", response_model=ManualActivityPageResponse)
async def list_activity(
    conversation_id: str,
    raven: RavenLease,
    after_sequence: Annotated[int, Query(ge=0, le=SQLITE_MAX_INTEGER)] = 0,
    limit: Annotated[int, Query(ge=1, le=199)] = 50,
    turn_id: UUID | None = None,
) -> ManualActivityPageResponse:
    """Read recorded actions, optionally limited to one committed turn."""
    async with _manual_session(raven, conversation_id) as session:
        records = await session.list_manual_activity(
            after_sequence=after_sequence, limit=limit + 1, turn_id=turn_id,
        )
    items = records[:limit]
    return ManualActivityPageResponse(
        items=items,
        next_cursor=items[-1].sequence if len(records) > limit else None,
    )


@router.delete("/{activity_id}", response_model=ManualActivityRemovalResponse)
async def remove_activity(
    conversation_id: str,
    activity_id: UUID,
    raven: RavenLease,
) -> ManualActivityRemovalResponse:
    """Remove a pending action that has not been captured by an agent turn."""
    async with _manual_session(raven, conversation_id) as session:
        removed = await session.remove_from_activity(activity_id)
    return ManualActivityRemovalResponse(removed=removed)


@router.post("/knowledges", status_code=status.HTTP_202_ACCEPTED)
async def list_knowledges(
    conversation_id: str, raven: RavenLease, request: Request,
) -> OperationTaskReference:
    """List knowledges in the user's knowledge base and record the action."""
    return await _submit_action(request, raven, conversation_id, Session.list_knowledges)


@router.post("/knowledges/{knowledge_name}/files", status_code=status.HTTP_202_ACCEPTED)
async def list_files(
    conversation_id: str, knowledge_name: str, raven: RavenLease, request: Request,
) -> OperationTaskReference:
    """List files in the named knowledge and record the action."""
    return await _submit_action(
        request, raven, conversation_id, lambda session: session.list_files(knowledge_name),
    )


@router.post(
    "/knowledges/{knowledge_name}/files/{file_id}/sections",
    status_code=status.HTTP_202_ACCEPTED,
)
async def list_sections(
    conversation_id: str,
    knowledge_name: str,
    file_id: str,
    raven: RavenLease,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    after_section_index: Annotated[int, Query(ge=0, le=SQLITE_MAX_INTEGER)] = 0,
    get_content: bool = False,
) -> OperationTaskReference:
    """List and record one page of sections, optionally with content."""
    return await _submit_action(
        request, raven, conversation_id,
        lambda session: session.list_sections(
            knowledge_name, file_id, limit=limit,
            after_section_index=after_section_index, get_content=get_content,
        ),
    )


@router.post("/knowledges/{knowledge_name}/files/{file_id}/open", status_code=status.HTTP_202_ACCEPTED)
async def open_file(
    conversation_id: str,
    knowledge_name: str,
    file_id: str,
    raven: RavenLease,
    request: Request,
    origin_activity_id: UUID | None = None,
    origin_turn_id: UUID | None = None,
) -> OperationTaskReference:
    """Open file metadata and record the inspection."""
    return await _submit_action(
        request, raven, conversation_id,
        lambda session: session.open_file(
            knowledge_name, file_id, origin_activity_id=origin_activity_id, origin_turn_id=origin_turn_id,
        ),
    )


@router.post("/knowledges/{knowledge_name}/sections/{section_id}/open", status_code=status.HTTP_202_ACCEPTED)
async def open_section(
    conversation_id: str,
    knowledge_name: str,
    section_id: str,
    raven: RavenLease,
    request: Request,
    origin_activity_id: UUID | None = None,
    origin_turn_id: UUID | None = None,
) -> OperationTaskReference:
    """Open a stored section and record the inspection."""
    return await _submit_action(
        request, raven, conversation_id,
        lambda session: session.open_section(
            knowledge_name, section_id, origin_activity_id=origin_activity_id, origin_turn_id=origin_turn_id,
        ),
    )


@router.post("/retrieval", status_code=status.HTTP_202_ACCEPTED)
async def retrieve(
    conversation_id: str, body: ManualRetrievalRequest, raven: RavenLease, request: Request,
) -> OperationTaskReference:
    """Run and record retrieval using the conversation's scope."""
    return await _submit_action(
        request, raven, conversation_id,
        lambda session: session.retrieve(body.query, retrieval_mode=body.retrieval_mode),
    )


@router.post("/reconstruction", status_code=status.HTTP_202_ACCEPTED)
async def reconstruct(
    conversation_id: str, body: SourceReconstructionRequest, raven: RavenLease, request: Request,
) -> OperationTaskReference:
    """Reconstruct selected sources and record their references."""
    sections = [section.model_dump() for section in body.sections]
    return await _submit_action(
        request, raven, conversation_id, lambda session: session.reconstruct(sections),
    )


async def _submit_action(
    request: Request,
    raven: Raven,
    conversation_id: str,
    action: Callable[[Session], Awaitable[OperationTask]],
) -> OperationTaskReference:
    conversation = raven.get_conversation(conversation_id)
    session = raven.session(conversation, manual_only=True)

    async def start_action() -> OperationTask:
        await session.start()
        return await action(session)

    task = await submit_task(request, raven, start_action, cleanup=session.close)
    return OperationTaskReference.from_task(task)


@asynccontextmanager
async def _manual_session(raven: Raven, conversation_id: str) -> AsyncGenerator[Session, None]:
    session = raven.session(raven.get_conversation(conversation_id), manual_only=True)
    try:
        await session.start()
        yield session
    finally:
        await session.close()
