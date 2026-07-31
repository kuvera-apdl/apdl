"""Strict project-scoped LLM provider connection and model discovery API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.auth import Principal, require_project
from app.llm.provider_catalog import (
    ProviderDiscoveryError,
    ProviderModel,
    discover_models,
)
from app.store.llm_connections import (
    ConnectionMetadata,
    LlmConnectionAssignmentConflictError,
    LlmConnectionAuthorizationError,
    LlmConnectionConflictError,
    LlmConnectionNotFoundError,
    ProjectConnectionStore,
)
from app.store.llm_credentials import CredentialStoreError


ProviderPath = Literal["openai", "anthropic", "google", "xai"]
PROJECT_PATTERN = r"^[A-Za-z0-9]{1,64}$"

router = APIRouter(prefix="/v1/agents/llm-connections", tags=["agents"])


class PutConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=PROJECT_PATTERN)
    api_key: SecretStr = Field(min_length=1, max_length=16_384)
    version: int = Field(ge=0)

    @field_validator("api_key")
    @classmethod
    def validate_api_key_size(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) > 16_384:
            raise ValueError("api_key must not exceed 16384 UTF-8 bytes")
        return value


class RefreshConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=PROJECT_PATTERN)
    version: int = Field(ge=1)


class RevokeConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=PROJECT_PATTERN)
    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2_000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if value != value.strip() or "\r" in value or "\n" in value:
            raise ValueError(
                "reason must not contain surrounding whitespace or line breaks"
            )
        return value


class ProviderModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["llm_provider_model@1"]
    provider: ProviderPath
    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    display_name: str
    supported_tiers: list[Literal["fast", "reasoning"]]
    catalog_version: str
    data_residency: Literal["ca", "us", "eu", "global"]
    allowed_data_classifications: list[
        Literal["public", "internal", "confidential", "restricted"]
    ]
    pricing_status: Literal["operator_review_required"]


class ConnectionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["llm_provider_connection@1"] = (
        "llm_provider_connection@1"
    )
    project_id: str = Field(pattern=PROJECT_PATTERN)
    provider: ProviderPath
    version: int = Field(ge=1)
    state: Literal["active", "revoked"]
    catalog_version: str
    validated_at: datetime
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None
    model_count: int = Field(ge=0, le=1_000)


class ConnectionDetailResponse(ConnectionSummaryResponse):
    models: list[ProviderModelResponse]


class ConnectionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["llm_provider_connection_list@1"] = (
        "llm_provider_connection_list@1"
    )
    project_id: str = Field(pattern=PROJECT_PATTERN)
    connections: list[ConnectionSummaryResponse]


class ModelInventoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["llm_provider_model_inventory@1"] = (
        "llm_provider_model_inventory@1"
    )
    project_id: str = Field(pattern=PROJECT_PATTERN)
    provider: ProviderPath
    connection_version: int = Field(ge=1)
    models: list[ProviderModelResponse]


def _summary(connection: ConnectionMetadata) -> ConnectionSummaryResponse:
    return ConnectionSummaryResponse(
        project_id=connection.project_id,
        provider=connection.provider,
        version=connection.version,
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
        supported_tiers=list(model.supported_tiers),
        catalog_version=model.catalog_version,
        data_residency=model.data_residency,
        allowed_data_classifications=list(model.allowed_data_classifications),
        pricing_status=model.pricing_status,
    )


def _store(request: Request) -> ProjectConnectionStore:
    return request.app.state.llm_connection_store


def _mutation_actor(request: Request, project_id: str) -> UUID:
    principal: Principal = request.state.principal
    if principal.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credential is not authorized for this project",
        )
    if principal.actor_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A current human session is required",
        )
    return UUID(principal.actor_user_id)


def _provider_error(exc: ProviderDiscoveryError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    )


def _store_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LlmConnectionAuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, LlmConnectionNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(
        exc,
        (
            LlmConnectionConflictError,
            LlmConnectionAssignmentConflictError,
        ),
    ):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(
        status_code=503,
        detail="LLM connection storage is unavailable",
    )


async def _preflight_mutation(
    request: Request,
    project_id: str,
) -> tuple[ProjectConnectionStore, UUID]:
    store = _store(request)
    actor_user_id = _mutation_actor(request, project_id)
    try:
        await store.assert_mutation_authority(project_id, actor_user_id)
    except LlmConnectionAuthorizationError as exc:
        raise _store_error(exc) from exc
    return store, actor_user_id


@router.get("", response_model=ConnectionListResponse)
async def list_connections(
    request: Request,
    project_id: str = Query(..., pattern=PROJECT_PATTERN),
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


@router.put("/{provider}", response_model=ConnectionDetailResponse)
async def put_connection(
    body: PutConnectionRequest,
    request: Request,
    provider: ProviderPath = Path(...),
) -> ConnectionDetailResponse:
    store, actor_user_id = await _preflight_mutation(request, body.project_id)
    api_key = body.api_key.get_secret_value()
    try:
        models = await discover_models(provider, api_key)
        connection = await store.put(
            body.project_id,
            provider,
            api_key,
            models,
            expected_version=body.version,
            actor_user_id=actor_user_id,
        )
    except ProviderDiscoveryError as exc:
        raise _provider_error(exc) from exc
    except (
        CredentialStoreError,
        LlmConnectionAuthorizationError,
        LlmConnectionConflictError,
        LlmConnectionAssignmentConflictError,
        LlmConnectionNotFoundError,
    ) as exc:
        raise _store_error(exc) from exc
    except Exception as exc:
        raise _store_error(exc) from exc
    finally:
        api_key = ""
    return ConnectionDetailResponse(
        **_summary(connection).model_dump(),
        models=[_model(model) for model in models],
    )


@router.get("/{provider}/models", response_model=ModelInventoryResponse)
async def get_models(
    request: Request,
    provider: ProviderPath = Path(...),
    project_id: str = Query(..., pattern=PROJECT_PATTERN),
) -> ModelInventoryResponse:
    require_project(request, project_id, "agents:read")
    try:
        connection, models = await _store(request).get_active_with_models(
            project_id,
            provider,
        )
    except (
        LlmConnectionNotFoundError,
        LlmConnectionConflictError,
    ) as exc:
        raise _store_error(exc) from exc
    except Exception as exc:
        raise _store_error(exc) from exc
    return ModelInventoryResponse(
        project_id=project_id,
        provider=provider,
        connection_version=connection.version,
        models=[_model(model) for model in models],
    )


@router.post(
    "/{provider}/refresh-models",
    response_model=ConnectionDetailResponse,
)
async def refresh_models(
    body: RefreshConnectionRequest,
    request: Request,
    provider: ProviderPath = Path(...),
) -> ConnectionDetailResponse:
    store, actor_user_id = await _preflight_mutation(request, body.project_id)
    api_key = ""
    credential = None
    try:
        credential = await store.credential_for_refresh(
            body.project_id,
            provider,
            expected_version=body.version,
        )
        api_key = credential.api_key
        models = await discover_models(provider, api_key)
        connection = await store.refresh(
            body.project_id,
            provider,
            models,
            expected_version=body.version,
            expected_credential_id=credential.credential_id,
            actor_user_id=actor_user_id,
        )
    except ProviderDiscoveryError as exc:
        raise _provider_error(exc) from exc
    except (
        CredentialStoreError,
        LlmConnectionAuthorizationError,
        LlmConnectionConflictError,
        LlmConnectionAssignmentConflictError,
        LlmConnectionNotFoundError,
    ) as exc:
        raise _store_error(exc) from exc
    except Exception as exc:
        raise _store_error(exc) from exc
    finally:
        api_key = ""
        credential = None
    return ConnectionDetailResponse(
        **_summary(connection).model_dump(),
        models=[_model(model) for model in models],
    )


@router.post("/{provider}/revoke", response_model=ConnectionSummaryResponse)
async def revoke_connection(
    body: RevokeConnectionRequest,
    request: Request,
    provider: ProviderPath = Path(...),
) -> ConnectionSummaryResponse:
    store, actor_user_id = await _preflight_mutation(request, body.project_id)
    try:
        connection = await store.revoke(
            body.project_id,
            provider,
            expected_version=body.version,
            actor_user_id=actor_user_id,
            reason=body.reason,
        )
    except (
        CredentialStoreError,
        LlmConnectionAuthorizationError,
        LlmConnectionConflictError,
        LlmConnectionAssignmentConflictError,
        LlmConnectionNotFoundError,
    ) as exc:
        raise _store_error(exc) from exc
    except Exception as exc:
        raise _store_error(exc) from exc
    return _summary(connection)
