"""Standalone retrieval and source reconstruction endpoints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status

from ...core.events import Event, EventType
from ...core.operations import Operation, OperationTask, OperationType
from ...h_api.raven import Raven
from ..dependencies import lease_raven, submit_task
from ..schemas import (
    AgreementRetrievalRequest,
    EmbeddedRetrievalRequest,
    ErrorResponse,
    HierarchicalRetrievalRequest,
    OperationTaskReference,
    SourceReconstructionRequest,
    VectorConditionedRetrievalRequest,
)


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}
router = APIRouter(prefix="/api/v1", tags=["retrieval and reconstruction"], responses=_ERROR_RESPONSES)
RavenLease = Annotated[Raven, Depends(lease_raven)]


@router.post("/retrieval/embedded", status_code=status.HTTP_202_ACCEPTED)
async def retrieve_embedded(
    body: EmbeddedRetrievalRequest, raven: RavenLease, request: Request,
) -> OperationTaskReference:
    """Find sections by vector similarity in the supplied scope."""
    return await _submit_retrieval(
        request, raven, OperationType.RETRIEVAL_EMBEDDED,
        lambda operation: raven.retrieve_embedded(
            body.query, scope=body.scope, top_k=body.top_k, operation=operation,
        ),
    )


@router.post("/retrieval/hierarchical", status_code=status.HTTP_202_ACCEPTED)
async def retrieve_hierarchical(
    body: HierarchicalRetrievalRequest, raven: RavenLease, request: Request,
) -> OperationTaskReference:
    """Rank section metadata in the supplied scope using the configured LLM."""
    return await _submit_retrieval(
        request, raven, OperationType.RETRIEVAL_HIERARCHICAL,
        lambda operation: raven.retrieve_hierarchical(
            body.query, scope=body.scope, top_k_section=body.top_k_section,
            top_k_knowledge=body.top_k_knowledge, full_retrieval=body.full_retrieval,
            max_per_read=body.max_per_read, anchor_count=body.anchor_count, operation=operation,
        ),
    )


@router.post("/retrieval/agreement", status_code=status.HTTP_202_ACCEPTED)
async def retrieve_agreement(
    body: AgreementRetrievalRequest, raven: RavenLease, request: Request,
) -> OperationTaskReference:
    """Compare embedded and hierarchical searches of the supplied scope."""
    return await _submit_retrieval(
        request, raven, OperationType.RETRIEVAL_AGREEMENT,
        lambda operation: raven.retrieve_agreement(
            body.query, scope=body.scope, top_k=body.top_k,
            top_k_knowledge=body.top_k_knowledge, full_retrieval=body.full_retrieval,
            max_per_read=body.max_per_read, anchor_count=body.anchor_count, operation=operation,
        ),
    )


@router.post("/retrieval/vector-conditioned", status_code=status.HTTP_202_ACCEPTED)
async def retrieve_vector_conditioned(
    body: VectorConditionedRetrievalRequest, raven: RavenLease, request: Request,
) -> OperationTaskReference:
    """Shortlist files in scope by embedding and rank their sections."""
    return await _submit_retrieval(
        request, raven, OperationType.RETRIEVAL_VECTOR_CONDITIONED,
        lambda operation: raven.retrieve_vector_conditioned(
            body.query, scope=body.scope, top_k_files=body.top_k_files,
            top_k_sections=body.top_k_sections, max_per_read=body.max_per_read,
            anchor_count=body.anchor_count, operation=operation,
        ),
    )


@router.post("/reconstruction", status_code=status.HTTP_202_ACCEPTED)
async def reconstruct(
    body: SourceReconstructionRequest, raven: RavenLease, request: Request,
) -> OperationTaskReference:
    """Reconstruct source files with the supplied sections highlighted."""
    sections = [section.model_dump() for section in body.sections]
    task = await submit_task(request, raven, lambda: raven.reconstruct(sections))
    return OperationTaskReference.from_task(task)


async def _submit_retrieval(
    request: Request,
    raven: Raven,
    strategy: OperationType,
    retrieve: Callable[[Operation], Awaitable[OperationTask]],
) -> OperationTaskReference:
    async def search_and_publish(operation: Operation) -> Any:
        retrieval_task = await retrieve(operation)
        result = await retrieval_task.result()
        operation.raise_if_cancelled()
        await operation.publish(Event(type=EventType.RETRIEVAL_RESULT, data={"result": result}))
        return result

    task = await submit_task(request, raven, lambda: raven.run_operation(strategy, search_and_publish))
    return OperationTaskReference.from_task(task)
