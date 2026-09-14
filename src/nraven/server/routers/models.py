"""Provider-neutral model loading and Ollama management endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status

from ...h_api.raven import Raven
from ...providers import ModelRole
from ..dependencies import lease_raven
from ..schemas import (
    ErrorResponse,
    ModelLoadRequest,
    OllamaModelDetailsResponse,
    OllamaModelInspectionResponse,
    OllamaModelRequest,
    OllamaModelsResponse,
    OllamaModelResponse,
    OllamaModelUnloadRequest,
    OllamaProviderStatusResponse,
    OperationTaskReference,
)


router = APIRouter(tags=["models"])
_ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


@router.post(
    "/api/v1/models/load",
    response_model=OperationTaskReference,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def load_models(
    request: ModelLoadRequest,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> OperationTaskReference:
    task = await raven.load(
        request.llm.to_domain(),
        request.embedding.to_domain(),
    )
    return OperationTaskReference.from_task(task)


@router.get(
    "/api/v1/providers/ollama/status",
    response_model=OllamaProviderStatusResponse,
    responses=_ERROR_RESPONSES,
)
async def ollama_status(
    raven: Annotated[Raven, Depends(lease_raven)],
) -> OllamaProviderStatusResponse:
    task = await raven.check_ollama_connection()
    await task.result()
    return OllamaProviderStatusResponse()


@router.get(
    "/api/v1/providers/ollama/models",
    response_model=OllamaModelsResponse,
    responses=_ERROR_RESPONSES,
)
async def list_ollama_models(
    raven: Annotated[Raven, Depends(lease_raven)],
) -> OllamaModelsResponse:
    task = await raven.list_ollama_models()
    result = await task.result()
    return OllamaModelsResponse(
        items=[_model_summary(item) for item in result]
    )


@router.post(
    "/api/v1/providers/ollama/models/inspect",
    response_model=OllamaModelInspectionResponse,
    responses=_ERROR_RESPONSES,
)
async def inspect_ollama_model(
    request: OllamaModelRequest,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> OllamaModelInspectionResponse:
    task = await raven.inspect_ollama_model(request.model)
    result = await task.result()
    return _model_inspection(request.model, result)


@router.post(
    "/api/v1/providers/ollama/models/pull",
    response_model=OperationTaskReference,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def pull_ollama_model(
    request: OllamaModelRequest,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> OperationTaskReference:
    task = await raven.pull_ollama_model(request.model)
    return OperationTaskReference.from_task(task)


@router.post(
    "/api/v1/providers/ollama/models/delete",
    response_model=OperationTaskReference,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def delete_ollama_model(
    request: OllamaModelRequest,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> OperationTaskReference:
    task = await raven.delete_ollama_model(request.model)
    return OperationTaskReference.from_task(task)


@router.post(
    "/api/v1/providers/ollama/models/unload",
    response_model=OperationTaskReference,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERROR_RESPONSES,
)
async def unload_ollama_model(
    request: OllamaModelUnloadRequest,
    raven: Annotated[Raven, Depends(lease_raven)],
) -> OperationTaskReference:
    if request.role == ModelRole.LLM:
        task = await raven.unload_ollama_llm(request.model)
    else:
        task = await raven.unload_ollama_embedding(request.model)
    return OperationTaskReference.from_task(task)


def _model_summary(value: dict[str, Any]) -> OllamaModelResponse:
    return OllamaModelResponse(
        model=_optional_string(value.get("model") or value.get("name")),
        modified_at=value.get("modified_at"),
        digest=_optional_string(value.get("digest")),
        size=value.get("size"),
        details=_model_details(value.get("details")),
    )


def _model_inspection(
    model: str,
    value: dict[str, Any],
) -> OllamaModelInspectionResponse:
    model_info = value.get("model_info")
    capabilities = value.get("capabilities")
    return OllamaModelInspectionResponse(
        model=model,
        modified_at=value.get("modified_at"),
        template=_optional_string(value.get("template")),
        modelfile=_optional_string(value.get("modelfile")),
        license=_optional_string(value.get("license")),
        details=_model_details(value.get("details")),
        model_info=dict(model_info) if isinstance(model_info, dict) else {},
        parameters=_optional_string(value.get("parameters")),
        capabilities=(
            [str(capability) for capability in capabilities]
            if isinstance(capabilities, list)
            else []
        ),
    )


def _model_details(value: Any) -> OllamaModelDetailsResponse | None:
    if not isinstance(value, dict):
        return None
    families = value.get("families")
    return OllamaModelDetailsResponse(
        parent_model=_optional_string(value.get("parent_model")),
        format=_optional_string(value.get("format")),
        family=_optional_string(value.get("family")),
        families=(
            [str(family) for family in families]
            if isinstance(families, (list, tuple))
            else None
        ),
        parameter_size=_optional_string(value.get("parameter_size")),
        quantization_level=_optional_string(value.get("quantization_level")),
    )


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)
