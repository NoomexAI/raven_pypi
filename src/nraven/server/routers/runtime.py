"""User-scoped Raven runtime status endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from ...h_api.raven import Raven
from ..dependencies import lease_raven
from ..schemas import (
    DiscoveryIssueResponse,
    DiscoveryIssuesResponse,
    ErrorResponse,
    RuntimeSettingsResetRequest,
    RuntimeSettingsResponse,
    RuntimeSettingsUpdateRequest,
    RuntimeStatusResponse,
)


router = APIRouter(prefix="/api/v1/runtime", tags=["runtime"])
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
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
    "/settings",
    response_model=RuntimeSettingsResponse,
    responses=_ERROR_RESPONSES,
)
async def get_runtime_settings(
    raven: Annotated[Raven, Depends(lease_raven)],
) -> RuntimeSettingsResponse:
    return RuntimeSettingsResponse.model_validate(raven.get_runtime_settings())


@router.patch(
    "/settings",
    response_model=RuntimeSettingsResponse,
    responses=_ERROR_RESPONSES,
)
async def update_runtime_settings(
    request: RuntimeSettingsUpdateRequest,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> RuntimeSettingsResponse:
    task = await raven.update_runtime_settings(
        request.changes(),
        expected_revision=request.expected_revision,
    )
    return RuntimeSettingsResponse.model_validate(await task.result())


@router.post(
    "/settings/reset",
    response_model=RuntimeSettingsResponse,
    responses=_ERROR_RESPONSES,
)
async def reset_runtime_settings(
    request: RuntimeSettingsResetRequest,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> RuntimeSettingsResponse:
    task = await raven.reset_runtime_settings(
        expected_revision=request.expected_revision,
    )
    return RuntimeSettingsResponse.model_validate(await task.result())


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
