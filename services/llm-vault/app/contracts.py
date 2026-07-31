"""Strict public and internal vault API contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictStr,
    field_validator,
    model_validator,
)


Provider = Literal["anthropic", "openai", "google", "xai"]
Consumer = Literal["agents", "codegen"]
PROVIDERS = ("anthropic", "openai", "google", "xai")
CONSUMERS = ("agents", "codegen")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateConnectionRequest(StrictModel):
    project_id: str = Field(pattern=r"^[A-Za-z0-9]{1,64}$")
    provider: Provider
    label: str = Field(min_length=1, max_length=80)
    api_key: SecretStr = Field(min_length=1, max_length=16_384)
    consumers: list[Consumer] = Field(min_length=1, max_length=2)

    @field_validator("label")
    @classmethod
    def normalized_label(cls, value: str) -> str:
        if value != value.strip() or "\r" in value or "\n" in value:
            raise ValueError("label must be normalized and single-line")
        return value

    @field_validator("api_key")
    @classmethod
    def bounded_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw.encode("utf-8")) > 16_384 or "\x00" in raw:
            raise ValueError("api_key must contain at most 16384 UTF-8 bytes")
        return value

    @field_validator("consumers")
    @classmethod
    def canonical_consumers(
        cls, value: list[Consumer]
    ) -> list[Consumer]:
        if len(set(value)) != len(value):
            raise ValueError("consumers must be unique")
        canonical = [item for item in CONSUMERS if item in value]
        if value != canonical:
            raise ValueError("consumers must use canonical agents, codegen order")
        return value


class ReplaceConnectionRequest(CreateConnectionRequest):
    version: int = Field(ge=1)


class RefreshConnectionRequest(StrictModel):
    project_id: str = Field(pattern=r"^[A-Za-z0-9]{1,64}$")
    version: int = Field(ge=1)


class RevokeConnectionRequest(RefreshConnectionRequest):
    reason: str = Field(min_length=1, max_length=2_000)

    @field_validator("reason")
    @classmethod
    def normalized_reason(cls, value: str) -> str:
        if value != value.strip() or "\r" in value or "\n" in value:
            raise ValueError("reason must be normalized and single-line")
        return value


class VaultProviderModel(StrictModel):
    schema_version: Literal["project_llm_provider_model@1"] = (
        "project_llm_provider_model@1"
    )
    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class ConnectionSummary(StrictModel):
    schema_version: Literal["project_llm_connection@1"] = (
        "project_llm_connection@1"
    )
    connection_id: UUID
    project_id: str = Field(pattern=r"^[A-Za-z0-9]{1,64}$")
    provider: Provider
    label: str
    version: int = Field(ge=1)
    inventory_version: int = Field(ge=1)
    state: Literal["active", "revoked"]
    consumers: tuple[Consumer, ...]
    validated_at: datetime
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None
    model_count: int = Field(ge=0, le=1_000)


class ConnectionDetail(ConnectionSummary):
    models: tuple[VaultProviderModel, ...]

    @model_validator(mode="after")
    def matching_count(self) -> "ConnectionDetail":
        if self.model_count != len(self.models):
            raise ValueError("model_count must match models")
        return self


class ConnectionList(StrictModel):
    schema_version: Literal["project_llm_connection_list@1"] = (
        "project_llm_connection_list@1"
    )
    project_id: str = Field(pattern=r"^[A-Za-z0-9]{1,64}$")
    connections: tuple[ConnectionSummary, ...]


class CredentialAccessRequest(StrictModel):
    schema_version: Literal["llm_credential_access_request@1"]
    project_id: str = Field(pattern=r"^[A-Za-z0-9]{1,64}$")
    provider: Provider
    consumer: Consumer
    execution_id: StrictStr = Field(min_length=1, max_length=256)
    purpose: StrictStr = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    expected_credential_id: UUID = Field(strict=False)
    expected_credential_version: int = Field(ge=1)

    @field_validator("execution_id")
    @classmethod
    def normalized_execution_id(cls, value: str) -> str:
        if value != value.strip() or "\r" in value or "\n" in value:
            raise ValueError("execution_id must be normalized and single-line")
        return value


class CredentialAccessResponse(StrictModel):
    schema_version: Literal["llm_credential_access@1"] = (
        "llm_credential_access@1"
    )
    access_id: UUID
    connection_id: UUID
    credential_id: UUID
    credential_version: int = Field(ge=1)
    provider: Provider
    api_key: StrictStr = Field(min_length=1, max_length=16_384, repr=False)


class ProjectModelsRequest(StrictModel):
    schema_version: Literal["llm_vault_model_projection_request@1"]
    provider: Provider
    model_ids: list[str] = Field(min_length=1, max_length=1_000)

    @field_validator("model_ids")
    @classmethod
    def canonical_model_ids(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("model_ids must be unique and sorted")
        return value


class AgentsProjectedModel(StrictModel):
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


class AgentsProjection(StrictModel):
    schema_version: Literal["agents_llm_model_projection@1"]
    catalog_version: str
    models: tuple[AgentsProjectedModel, ...]


class CodegenProjectedModel(StrictModel):
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


class CodegenProjection(StrictModel):
    schema_version: Literal["codegen_llm_model_projection@1"]
    catalog_version: str
    models: tuple[CodegenProjectedModel, ...]
