"""Strict owner/delegate Agents project activation API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth import Principal, require_project
from app.store.llm_setup import (
    AgentsProjectSetup,
    AgentsSetupAuthorizationError,
    AgentsSetupConflictError,
    AgentsSetupNotFoundError,
    AgentsSetupStore,
    AgentsSetupValidationError,
    ModelSelection,
)


ProviderPath = Literal["openai", "anthropic", "google", "xai"]
PROJECT_PATTERN = r"^[A-Za-z0-9]{1,64}$"
MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"

router = APIRouter(prefix="/v1/agents/setup", tags=["agents"])


class ModelSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ProviderPath
    model: str = Field(pattern=MODEL_PATTERN)
    connection_version: int = Field(ge=1)
    inventory_version: int = Field(ge=1)


class PutAgentsSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=PROJECT_PATTERN)
    fast_model: ModelSelectionRequest
    reasoning_model: ModelSelectionRequest
    version: int = Field(ge=0)


class DeactivateAgentsSetupRequest(BaseModel):
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


class SetupAssignmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: Literal["fast", "reasoning"]
    provider: ProviderPath
    model: str = Field(pattern=MODEL_PATTERN)
    connection_version: int = Field(ge=1)
    inventory_version: int = Field(ge=1)
    model_catalog_version: str
    display_name: str
    endpoint_url: str
    endpoint_host: str
    data_residency: Literal["ca", "us", "eu", "global"]
    allowed_data_classifications: list[
        Literal["public", "internal", "confidential", "restricted"]
    ]
    input_cost_per_million_tokens_usd_micros: int = Field(ge=0)
    output_cost_per_million_tokens_usd_micros: int = Field(ge=0)
    current: bool
    assigned_at: datetime
    updated_at: datetime


class SetupConnectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ProviderPath
    connection_version: int = Field(ge=1)
    inventory_version: int = Field(ge=1)
    state: Literal["active", "revoked"]
    catalog_version: str
    current: bool
    validated_at: datetime


class SetupCallerCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    can_read: Literal[True] = True
    can_manage: bool
    can_activate: bool
    can_deactivate: bool
    management_authority: Literal["owner", "delegated", "none"]


class SetupPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_data_residency: Literal["local", "ca", "us", "eu", "global"]
    allow_cross_vendor_retry: Literal[False]
    project_daily_cost_limit_usd_micros: int = Field(ge=0)
    run_cost_limit_usd_micros: int = Field(ge=0)


class EffectfulExecutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authorized: bool
    authorization_source: (
        Literal["operator_provisioned", "self_registered_override"] | None
    )


class _SetupResponseBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agents_project_setup@1"] = (
        "agents_project_setup@1"
    )
    project_id: str = Field(pattern=PROJECT_PATTERN)
    version: int = Field(ge=0)
    caller_capabilities: SetupCallerCapabilities
    assignments: list[SetupAssignmentResponse]
    connections: list[SetupConnectionResponse]
    blockers: list[
        Literal[
            "project_inactive",
            "fast_model_required",
            "reasoning_model_required",
            "connection_inactive",
            "connection_stale",
            "inventory_stale",
            "model_unavailable",
            "model_ineligible",
            "catalog_stale",
            "credential_unavailable",
            "budget_invalid",
        ]
    ]
    analysis_ready: bool
    policy: SetupPolicyResponse
    effectful_execution: EffectfulExecutionResponse
    activated_at: datetime | None
    deactivated_at: datetime | None
    deactivation_reason: str | None


class InactiveSetupResponse(_SetupResponseBase):
    state: Literal["inactive"]


class ActiveSetupResponse(_SetupResponseBase):
    state: Literal["active"]


AgentsSetupResponse = Annotated[
    InactiveSetupResponse | ActiveSetupResponse,
    Field(discriminator="state"),
]


def _actor_user_id(request: Request, project_id: str) -> UUID | None:
    principal: Principal = request.state.principal
    if principal.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credential is not authorized for this project",
        )
    return (
        UUID(principal.actor_user_id)
        if principal.actor_user_id is not None
        else None
    )


def _mutation_actor_user_id(request: Request, project_id: str) -> UUID:
    actor_user_id = _actor_user_id(request, project_id)
    if actor_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A current human session is required",
        )
    return actor_user_id


def _store(request: Request) -> AgentsSetupStore:
    return request.app.state.agents_setup_store


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AgentsSetupAuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, AgentsSetupNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, AgentsSetupConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AgentsSetupValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(
        status_code=503,
        detail="Agents project setup storage is unavailable",
    )


def _response(setup: AgentsProjectSetup) -> dict[str, object]:
    return {
        "schema_version": "agents_project_setup@1",
        "project_id": setup.project_id,
        "state": setup.state,
        "version": setup.version,
        "caller_capabilities": {
            "can_read": True,
            "can_manage": setup.can_manage,
            "can_activate": setup.can_manage and setup.state == "inactive",
            "can_deactivate": setup.can_manage and setup.state == "active",
            "management_authority": setup.management_authority,
        },
        "assignments": [
            {
                "tier": assignment.tier,
                "provider": assignment.provider,
                "model": assignment.model,
                "connection_version": assignment.connection_version,
                "inventory_version": assignment.inventory_version,
                "model_catalog_version": assignment.model_catalog_version,
                "display_name": assignment.display_name,
                "endpoint_url": assignment.endpoint_url,
                "endpoint_host": assignment.endpoint_host,
                "data_residency": assignment.data_residency,
                "allowed_data_classifications": list(
                    assignment.allowed_data_classifications
                ),
                "input_cost_per_million_tokens_usd_micros": (
                    assignment.input_cost_per_million_tokens_usd_micros
                ),
                "output_cost_per_million_tokens_usd_micros": (
                    assignment.output_cost_per_million_tokens_usd_micros
                ),
                "current": assignment.current,
                "assigned_at": assignment.assigned_at,
                "updated_at": assignment.updated_at,
            }
            for assignment in setup.assignments
        ],
        "connections": [
            {
                "provider": connection.provider,
                "connection_version": connection.connection_version,
                "inventory_version": connection.inventory_version,
                "state": connection.state,
                "catalog_version": connection.catalog_version,
                "current": connection.current,
                "validated_at": connection.validated_at,
            }
            for connection in setup.connections
        ],
        "blockers": list(setup.blockers),
        "analysis_ready": setup.analysis_ready,
        "policy": {
            "required_data_residency": setup.required_data_residency,
            "allow_cross_vendor_retry": setup.allow_cross_vendor_retry,
            "project_daily_cost_limit_usd_micros": (
                setup.project_daily_cost_limit_usd_micros
            ),
            "run_cost_limit_usd_micros": (
                setup.run_cost_limit_usd_micros
            ),
        },
        "effectful_execution": {
            "authorized": setup.effectful_execution_authorized,
            "authorization_source": (
                setup.effectful_execution_authorization_source
            ),
        },
        "activated_at": setup.activated_at,
        "deactivated_at": setup.deactivated_at,
        "deactivation_reason": setup.deactivation_reason,
    }


@router.get("", response_model=AgentsSetupResponse)
async def get_agents_setup(
    request: Request,
    project_id: str = Query(..., pattern=PROJECT_PATTERN),
) -> dict[str, object]:
    require_project(request, project_id, "agents:read")
    try:
        setup = await _store(request).get(
            project_id,
            actor_user_id=_actor_user_id(request, project_id),
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _response(setup)


@router.put("", response_model=AgentsSetupResponse)
async def put_agents_setup(
    body: PutAgentsSetupRequest,
    request: Request,
) -> dict[str, object]:
    actor_user_id = _mutation_actor_user_id(request, body.project_id)
    try:
        setup = await _store(request).put(
            body.project_id,
            fast_model=ModelSelection(**body.fast_model.model_dump()),
            reasoning_model=ModelSelection(
                **body.reasoning_model.model_dump()
            ),
            expected_version=body.version,
            actor_user_id=actor_user_id,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _response(setup)


@router.post("/deactivate", response_model=AgentsSetupResponse)
async def deactivate_agents_setup(
    body: DeactivateAgentsSetupRequest,
    request: Request,
) -> dict[str, object]:
    actor_user_id = _mutation_actor_user_id(request, body.project_id)
    try:
        setup = await _store(request).deactivate(
            body.project_id,
            expected_version=body.version,
            actor_user_id=actor_user_id,
            reason=body.reason,
        )
    except Exception as exc:
        raise _error(exc) from exc
    return _response(setup)
