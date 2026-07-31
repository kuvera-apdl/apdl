"""Read-only Codegen projection of vault-managed project LLM connections."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth import require_project
from app.llm.provider_catalog import ProviderModel
from app.store.llm_connections import (
    ConnectionMetadata,
    LlmConnectionConflictError,
    LlmConnectionNotFoundError,
    ProjectConnectionStore,
)


ProviderPath = Literal["openai", "anthropic", "google", "xai"]
PROJECT_PATTERN = r"^[A-Za-z0-9]{1,64}$"
router = APIRouter(prefix="/v1/llm-connections", tags=["llm-connections"])


class ProviderModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["codegen_provider_model@1"]
    provider: ProviderPath
    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    display_name: str
    supported_roles: list[Literal["editor", "helper"]]
    catalog_version: str
    context_window_tokens: int = Field(gt=0)
    supports_tool_calling: bool
    supports_structured_output: bool
    data_residency: Literal["ca", "us", "eu", "global"]
    allowed_data_classifications: list[
        Literal["public", "internal", "confidential", "restricted"]
    ]
    input_cost_per_million_tokens_usd_micros: int = Field(ge=0)
    output_cost_per_million_tokens_usd_micros: int = Field(ge=0)
    pricing_status: Literal["catalog_reviewed"]


class ConnectionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["codegen_provider_connection@1"] = (
        "codegen_provider_connection@1"
    )
    project_id: str = Field(pattern=PROJECT_PATTERN)
    provider: ProviderPath
    version: int = Field(ge=1)
    inventory_version: int = Field(ge=1)
    state: Literal["active", "revoked"]
    catalog_version: str
    validated_at: datetime
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None
    model_count: int = Field(ge=0, le=1_000)


class ConnectionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["codegen_provider_connection_list@1"] = (
        "codegen_provider_connection_list@1"
    )
    project_id: str = Field(pattern=PROJECT_PATTERN)
    connections: list[ConnectionSummaryResponse]


class ModelInventoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["codegen_provider_model_inventory@1"] = (
        "codegen_provider_model_inventory@1"
    )
    project_id: str = Field(pattern=PROJECT_PATTERN)
    provider: ProviderPath
    connection_version: int = Field(ge=1)
    inventory_version: int = Field(ge=1)
    models: list[ProviderModelResponse]


def _summary(connection: ConnectionMetadata) -> ConnectionSummaryResponse:
    return ConnectionSummaryResponse(
        project_id=connection.project_id,
        provider=connection.provider,
        version=connection.version,
        inventory_version=connection.inventory_version,
        state=connection.state,
        catalog_version=connection.catalog_version,
        validated_at=connection.validated_at,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
        revoked_at=connection.revoked_at,
        model_count=connection.model_count,
    )


def _model(model: ProviderModel) -> ProviderModelResponse:
    return ProviderModelResponse(
        schema_version=model.schema_version,
        provider=model.provider,
        model_id=model.model_id,
        display_name=model.display_name,
        supported_roles=list(model.supported_roles),
        catalog_version=model.catalog_version,
        context_window_tokens=model.context_window_tokens,
        supports_tool_calling=model.supports_tool_calling,
        supports_structured_output=model.supports_structured_output,
        data_residency=model.data_residency,
        allowed_data_classifications=list(model.allowed_data_classifications),
        input_cost_per_million_tokens_usd_micros=(
            model.input_cost_per_million_tokens_usd_micros
        ),
        output_cost_per_million_tokens_usd_micros=(
            model.output_cost_per_million_tokens_usd_micros
        ),
        pricing_status=model.pricing_status,
    )


def _store(request: Request) -> ProjectConnectionStore:
    return request.app.state.llm_connection_store


def _store_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LlmConnectionNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, LlmConnectionConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(
        status_code=503, detail="LLM connection projection is unavailable"
    )


@router.get("", response_model=ConnectionListResponse)
async def list_connections(
    request: Request,
    project_id: str = Query(pattern=PROJECT_PATTERN),
) -> ConnectionListResponse:
    require_project(request, project_id, "agents:read")
    try:
        connections = await _store(request).list(project_id)
    except Exception as exc:
        raise _store_error(exc) from exc
    return ConnectionListResponse(
        project_id=project_id,
        connections=[_summary(connection) for connection in connections],
    )


@router.get("/{provider}/models", response_model=ModelInventoryResponse)
async def get_models(
    request: Request,
    provider: ProviderPath = Path(),
    project_id: str = Query(pattern=PROJECT_PATTERN),
) -> ModelInventoryResponse:
    require_project(request, project_id, "agents:read")
    try:
        connection, models = await _store(request).get_active_with_models(
            project_id, provider
        )
    except Exception as exc:
        raise _store_error(exc) from exc
    return ModelInventoryResponse(
        project_id=project_id,
        provider=provider,
        connection_version=connection.version,
        inventory_version=connection.inventory_version,
        models=[_model(model) for model in models],
    )
