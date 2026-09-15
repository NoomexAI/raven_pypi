"""Persistent conversation, canonical transcript, and agent-turn endpoints."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from ...agent.policy import LOCAL_RETRIEVAL_MODES, RetrievalMode
from ...core.errors import ErrorCode, RavenError
from ...h_api.raven import Raven
from ..dependencies import lease_raven
from ..schemas import (
    ConversationCreateRequest,
    ConversationMessagePageResponse,
    ConversationMessageResponse,
    ConversationPageResponse,
    ConversationResponse,
    ConversationUpdateRequest,
    ErrorResponse,
    OperationTaskReference,
    PreferenceCreateRequest,
    PreferenceResponse,
    PreferencesResponse,
    TurnAcceptedResponse,
    TurnCreateRequest,
    TurnResponse,
)


router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


@router.get("", response_model=ConversationPageResponse, responses=_ERROR_RESPONSES)
async def list_conversations(
    raven: Annotated[Raven, Depends(lease_raven)],
    limit: Annotated[int | None, Query(ge=1)] = None,
    after_conversation_id: str | None = None,
) -> ConversationPageResponse:
    page_size = _page_size(raven, limit)
    records = raven.list_conversations_page(
        limit=page_size + 1,
        after_conversation_id=after_conversation_id,
    )
    has_more = len(records) > page_size
    selected = records[:page_size]
    return ConversationPageResponse(
        items=[ConversationResponse.model_validate(item) for item in selected],
        next_cursor=(
            selected[-1]["conversation_id"] if has_more and selected else None
        ),
    )


@router.post(
    "",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
async def create_conversation(
    request: ConversationCreateRequest,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> ConversationResponse:
    task = await raven.create_conversation(request.knowledge_name)
    conversation = await task.result()
    return ConversationResponse.model_validate(conversation.to_dict())


@router.get(
    "/{conversation_id}",
    response_model=ConversationResponse,
    responses=_ERROR_RESPONSES,
)
async def get_conversation(
    conversation_id: str,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> ConversationResponse:
    return ConversationResponse.model_validate(
        raven.get_conversation_details(conversation_id)
    )


@router.patch(
    "/{conversation_id}",
    response_model=ConversationResponse,
    responses=_ERROR_RESPONSES,
)
async def update_conversation(
    conversation_id: str,
    request: ConversationUpdateRequest,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> ConversationResponse:
    task = await raven.update_conversation(
        conversation_id,
        title=request.title,
        pinned=request.pinned,
    )
    await task.result()
    return ConversationResponse.model_validate(
        raven.get_conversation_details(conversation_id)
    )


@router.delete(
    "/{conversation_id}",
    response_model=OperationTaskReference,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def delete_conversation(
    conversation_id: str,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> OperationTaskReference:
    raven.get_conversation(conversation_id)
    task = await raven.delete_conversation(conversation_id)
    return OperationTaskReference.from_task(task)


@router.get(
    "/{conversation_id}/messages",
    response_model=ConversationMessagePageResponse,
    responses=_ERROR_RESPONSES,
)
async def get_messages(
    conversation_id: str,
    raven: Annotated[Raven, Depends(lease_raven)],
    limit: Annotated[int | None, Query(ge=1)] = None,
    after_message_id: Annotated[int, Query(ge=0)] = 0,
) -> ConversationMessagePageResponse:
    page_size = _page_size(raven, limit)
    records = await raven.get_conversation_messages_page(
        conversation_id,
        limit=page_size + 1,
        after_message_id=after_message_id,
    )
    return _message_page(records, page_size)


@router.get(
    "/{conversation_id}/turns/{turn_id}",
    response_model=TurnResponse,
    responses=_ERROR_RESPONSES,
)
async def get_turn(
    conversation_id: str,
    turn_id: UUID,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> TurnResponse:
    record = await raven.get_conversation_turn(conversation_id, turn_id)
    return TurnResponse.model_validate(record)


@router.get(
    "/{conversation_id}/turns/{turn_id}/messages",
    response_model=ConversationMessagePageResponse,
    responses=_ERROR_RESPONSES,
)
async def get_turn_messages(
    conversation_id: str,
    turn_id: UUID,
    raven: Annotated[Raven, Depends(lease_raven)],
    limit: Annotated[int | None, Query(ge=1)] = None,
    after_message_id: Annotated[int, Query(ge=0)] = 0,
) -> ConversationMessagePageResponse:
    page_size = _page_size(raven, limit)
    records = await raven.get_conversation_turn_messages_page(
        conversation_id,
        turn_id,
        limit=page_size + 1,
        after_message_id=after_message_id,
    )
    return _message_page(records, page_size)


@router.post(
    "/{conversation_id}/turns",
    response_model=TurnAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def submit_turn(
    conversation_id: str,
    request: TurnCreateRequest,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> TurnAcceptedResponse:
    conversation = raven.get_conversation(conversation_id)
    selected_mode = request.retrieval_mode
    if (
        conversation.type == "local"
        and isinstance(selected_mode, RetrievalMode)
        and selected_mode not in LOCAL_RETRIEVAL_MODES
    ):
        raise RavenError(
            ErrorCode.INVALID_RETRIEVAL_MODE,
            f"Retrieval mode '{selected_mode.value}' is not valid for a local conversation.",
        )
    session = raven.session(conversation)
    await session.start()
    run = await session.generate_response(
        request.user_query,
        retrieval_mode=request.retrieval_mode,
    )
    return TurnAcceptedResponse(
        conversation_id=conversation_id,
        turn_id=run.turn_id,
        operation_id=run.operation_id,
        task_id=run.task.task_id,
        status=run.status,
        events_url=f"/api/v1/operations/{run.operation_id}/events",
    )


@router.post(
    "/{conversation_id}/turns/{turn_id}/reconstruction",
    response_model=OperationTaskReference,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def reconstruct_turn(
    conversation_id: str,
    turn_id: UUID,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> OperationTaskReference:
    await raven.get_conversation_turn(conversation_id, turn_id)
    task = await raven.reconstruct_from_turn(conversation_id, turn_id)
    return OperationTaskReference.from_task(task)


@router.get(
    "/{conversation_id}/preferences",
    response_model=PreferencesResponse,
    responses=_ERROR_RESPONSES,
)
async def list_preferences(
    conversation_id: str,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> PreferencesResponse:
    records = await raven.get_conversation_preferences(conversation_id)
    return PreferencesResponse(
        items=[PreferenceResponse.model_validate(item) for item in records]
    )


@router.post(
    "/{conversation_id}/preferences",
    response_model=PreferenceResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
async def save_preference(
    conversation_id: str,
    request: PreferenceCreateRequest,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> PreferenceResponse:
    task = await raven.save_conversation_preference(conversation_id, request.text)
    return PreferenceResponse.model_validate(await task.result())


@router.delete(
    "/{conversation_id}/preferences/{preference_id}",
    response_model=PreferenceResponse,
    responses=_ERROR_RESPONSES,
)
async def remove_preference(
    conversation_id: str,
    preference_id: UUID,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> PreferenceResponse:
    task = await raven.remove_conversation_preference(
        conversation_id,
        str(preference_id),
    )
    return PreferenceResponse.model_validate(await task.result())


def _message_page(
    records: list[dict[str, Any]],
    page_size: int,
) -> ConversationMessagePageResponse:
    has_more = len(records) > page_size
    selected = records[:page_size]
    return ConversationMessagePageResponse(
        items=[ConversationMessageResponse.from_record(item) for item in selected],
        next_cursor=(selected[-1]["message_id"] if has_more and selected else None),
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
