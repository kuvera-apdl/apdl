"""Strict immutable Codegen LLM assignment and runtime contracts."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    model_validator,
)

from app.models.execution import canonical_sha256


Provider = Literal["anthropic", "openai", "google", "xai"]
Role = Literal["editor", "helper"]
Phase = Literal["brief", "edit", "review", "repair"]


class LlmAssignmentSnapshot(BaseModel):
    """One model assignment frozen when a changeset is admitted."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["codegen_llm_assignment_snapshot@1"]
    role: Role
    provider: Provider
    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    assignment_version: int = Field(ge=1)
    connection_version: int = Field(ge=1)
    inventory_version: int = Field(ge=1)
    catalog_version: str = Field(
        pattern=r"^codegen-provider-catalog@[1-9][0-9]*$"
    )
    context_window_tokens: int = Field(ge=16_000)
    supports_tool_calling: bool
    supports_structured_output: bool
    input_cost_per_million_tokens_usd_micros: int = Field(ge=0)
    output_cost_per_million_tokens_usd_micros: int = Field(ge=0)


class LlmExecutionSnapshot(BaseModel):
    """Repository-bound, immutable routing authority for one changeset."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["codegen_llm_execution_snapshot@2"]
    project_id: str = Field(pattern=r"^[A-Za-z0-9]{1,64}$")
    repository_grant_id: str = Field(
        min_length=5,
        max_length=132,
        pattern=r"^ghg_[A-Za-z0-9_-]+$",
    )
    repository_id: int = Field(ge=1)
    repository_installation_id: int = Field(ge=1)
    repository_full_name: str = Field(
        max_length=201,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
    )
    codegen_revision: str = Field(min_length=1, max_length=200)
    behavior_configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollout_stage: Literal[
        "offline",
        "development_pr",
        "tenant_draft_pr",
    ]
    assignments: tuple[LlmAssignmentSnapshot, LlmAssignmentSnapshot]

    @model_validator(mode="after")
    def require_exact_roles(self) -> LlmExecutionSnapshot:
        if self.codegen_revision != self.codegen_revision.strip():
            raise ValueError("codegen_revision must be normalized")
        if tuple(item.role for item in self.assignments) != ("editor", "helper"):
            raise ValueError(
                "assignments must contain editor then helper exactly once"
            )
        return self

    def assignment(self, role: Role) -> LlmAssignmentSnapshot:
        return self.assignments[0 if role == "editor" else 1]

    def evidence_sha256(self) -> str:
        """Content-address the exact repository and assignment authority."""
        return canonical_sha256(self)


class LlmRuntimeBinding(BaseModel):
    """Ephemeral provider authority serialized to the worker in plaintext."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    role: Role
    provider: Provider
    model_id: str
    litellm_model: str
    credential_environment_name: str
    endpoint_url: str
    assignment_version: int
    credential_id: UUID
    credential_version: int
    input_cost_per_million_tokens_usd_micros: int = Field(ge=0)
    output_cost_per_million_tokens_usd_micros: int = Field(ge=0)
    api_key: StrictStr = Field(
        min_length=1,
        max_length=16_384,
        repr=False,
        description=(
            "Plaintext attempt-scoped provider credential. repr=False is log "
            "hygiene only; normal model serialization includes this field."
        ),
    )


class LlmExecutionAuthority(BaseModel):
    """Ephemeral capability for a worker to request one phase at a time."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["codegen_llm_execution_authority@1"] = (
        "codegen_llm_execution_authority@1"
    )
    socket_path: StrictStr = Field(
        min_length=1,
        max_length=103,
        pattern=r"^/[^\x00\r\n]+$",
    )
    token: StrictStr = Field(
        min_length=43,
        max_length=128,
        repr=False,
    )
    editor_model: StrictStr = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$"
    )
    helper_model: StrictStr = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$"
    )
    allowed_phases: tuple[Phase, ...]

    @model_validator(mode="after")
    def require_exact_phases(self) -> LlmExecutionAuthority:
        if self.allowed_phases not in (
            ("brief", "edit", "review"),
            ("brief", "repair", "review"),
        ):
            raise ValueError(
                "authority requires brief/edit/review or "
                "brief/repair/review phases"
            )
        return self


class PreparedLlmAttempt(BaseModel):
    """Durable pre-egress attempt plus worker-bound plaintext authority."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    attempt_id: UUID
    changeset_id: str
    project_id: str
    phase: Phase
    attempt_sequence: int
    binding: LlmRuntimeBinding


class LlmAttemptLease(BaseModel):
    """One worker-held phase lease containing a plaintext provider credential."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["codegen_llm_attempt_lease@1"] = (
        "codegen_llm_attempt_lease@1"
    )
    attempt_id: UUID
    phase: Phase
    binding: LlmRuntimeBinding
