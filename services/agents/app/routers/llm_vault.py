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

    schema_version: Literal["llm_provider_model@1"]
    provider: Provider
    model_id: str
    display_name: str
    supported_tiers: tuple[Literal["fast", "reasoning"], ...]
    catalog_version: str
    data_residency: Literal["ca", "us", "eu", "global"]
    allowed_data_classifications: tuple[
        Literal["public", "internal", "confidential", "restricted"], ...
    ]
    pricing_status: Literal["catalog_reviewed"]


class ProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["agents_llm_model_projection@1"] = (
        "agents_llm_model_projection@1"
    )
    catalog_version: str
    models: tuple[ProjectedModel, ...]


def _authorize(authorization: str) -> None:
    expected = os.getenv("LLM_VAULT_PROJECTION_TOKEN", "")
    supplied = authorization.removeprefix("Bearer ")
    if (
        not authorization.startswith("Bearer ")
        or len(expected.encode("utf-8")) < 32
        or not secrets.compare_digest(supplied, expected)
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
        supported_tiers=model.supported_tiers,
        catalog_version=model.catalog_version,
        data_residency=model.data_residency,
        allowed_data_classifications=model.allowed_data_classifications,
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
