"""User-scoped Raven runtime status endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ...h_api.raven import Raven
from ..dependencies import lease_raven
from ..schemas import (
    DiscoveryIssueResponse,
    DiscoveryIssuesResponse,
    ErrorResponse,
    RuntimeStatusResponse,
)


router = APIRouter(prefix="/api/v1/runtime", tags=["runtime"])
_ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


@router.get(
    "",
    response_model=RuntimeStatusResponse,
    responses=_ERROR_RESPONSES,
)
async def runtime_status(
    raven: Annotated[Raven, Depends(lease_raven)],
) -> RuntimeStatusResponse:
    return RuntimeStatusResponse.model_validate(raven.runtime_status())


@router.get(
    "/discovery-issues",
    response_model=DiscoveryIssuesResponse,
    responses=_ERROR_RESPONSES,
)
async def list_discovery_issues(
    raven: Annotated[Raven, Depends(lease_raven)],
) -> DiscoveryIssuesResponse:
    return DiscoveryIssuesResponse(
        items=[
            DiscoveryIssueResponse.model_validate(issue.model_dump())
            for issue in raven.list_discovery_issues()
        ]
    )
