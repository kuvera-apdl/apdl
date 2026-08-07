"""Pure model-catalog projection API for the private LLM vault."""

from __future__ import annotations

import os
import secrets
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.llm.provider_catalog import CATALOG_VERSION, ProviderModel, catalog_model


Provider = Literal["anthropic", "openai", "google", "xai"]
router = APIRouter(prefix="/internal/v1/llm-vault", tags=["llm-vault"])


class ProjectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["llm_vault_model_projection_request@1"]
    provider: Provider
    model_ids: list[str] = Field(min_length=1, max_length=1_000)

    @field_validator("model_ids")
    @classmethod
    def canonical_model_ids(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("model_ids must be unique and sorted")
        return value


class ProjectedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["codegen_provider_model@1"]
    provider: Provider
    model_id: str
    display_name: str
    supported_roles: tuple[Literal["editor", "helper"], ...]
    catalog_version: str
    context_window_tokens: int = Field(ge=16_000)
    supports_tool_calling: bool
    supports_structured_output: bool
    data_residency: Literal["ca", "us", "eu", "global"]
    allowed_data_classifications: tuple[
        Literal["public", "internal", "confidential", "restricted"], ...
    ]
    input_cost_per_million_tokens_usd_micros: int = Field(ge=0)
    output_cost_per_million_tokens_usd_micros: int = Field(ge=0)
    pricing_status: Literal["catalog_reviewed"]


class ProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["codegen_llm_model_projection@1"] = (
        "codegen_llm_model_projection@1"
    )
    catalog_version: str
    models: tuple[ProjectedModel, ...]


def _constant_time_header_equal(candidate: str, expected: str) -> bool:
    try:
        candidate_bytes = candidate.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return secrets.compare_digest(candidate_bytes, expected.encode("utf-8"))


def _authorize(authorization: str) -> None:
    expected = os.getenv("LLM_VAULT_PROJECTION_TOKEN", "")
    supplied = authorization.removeprefix("Bearer ")
    if (
        not authorization.startswith("Bearer ")
        or len(expected.encode("utf-8")) < 32
        or not _constant_time_header_equal(supplied, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Vault projection authentication failed",
        )


def _project(model: ProviderModel) -> ProjectedModel:
    return ProjectedModel(
        schema_version=model.schema_version,
        provider=model.provider,
        model_id=model.model_id,
        display_name=model.display_name,
        supported_roles=model.supported_roles,
        catalog_version=model.catalog_version,
        context_window_tokens=model.context_window_tokens,
        supports_tool_calling=model.supports_tool_calling,
        supports_structured_output=model.supports_structured_output,
        data_residency=model.data_residency,
        allowed_data_classifications=model.allowed_data_classifications,
        input_cost_per_million_tokens_usd_micros=(
            model.input_cost_per_million_tokens_usd_micros
        ),
        output_cost_per_million_tokens_usd_micros=(
            model.output_cost_per_million_tokens_usd_micros
        ),
        pricing_status=model.pricing_status,
    )


@router.post("/project-models", response_model=ProjectionResponse)
async def project_models(
    body: ProjectionRequest,
    authorization: str = Header(default=""),
) -> ProjectionResponse:
    _authorize(authorization)
    models = tuple(
        _project(model)
        for model_id in body.model_ids
        if (model := catalog_model(body.provider, model_id)) is not None
    )
    return ProjectionResponse(catalog_version=CATALOG_VERSION, models=models)
