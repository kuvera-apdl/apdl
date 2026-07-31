"""Tenant-scoped publication gates at the GitHub credential boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.llm.contracts import LlmExecutionSnapshot
from app.llm.provider_catalog import CATALOG_VERSION, runtime_model
from app.models.execution import (
    DockerImageId,
    PublicationStage,
    RiskLevel,
    Sha256,
    canonical_sha256,
)


DEVELOPMENT_CODEGEN_REVISION = "local-development"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TenantPublicationRuntimeIdentity(_StrictModel):
    """Content identity of the production worker and enforced egress runtime."""

    schema_version: Literal["tenant_publication_runtime_identity@1"]
    controller_image_id: DockerImageId
    worker_image_id: DockerImageId
    codegen_revision: str = Field(min_length=1, max_length=200)
    behavior_configuration_sha256: Sha256
    egress_policy_sha256: Sha256
    egress_proxy_image_id: DockerImageId
    egress_transport: Literal["network_none_unix_socket@1"]
    max_concurrent_jobs: Literal[1]
    identity_sha256: Sha256

    @classmethod
    def build(
        cls,
        *,
        controller_image_id: str,
        worker_image_id: str,
        codegen_revision: str,
        behavior_configuration_sha256: str,
        egress_policy_sha256: str,
        egress_proxy_image_id: str,
        max_concurrent_jobs: int,
    ) -> TenantPublicationRuntimeIdentity:
        payload = {
            "schema_version": "tenant_publication_runtime_identity@1",
            "controller_image_id": controller_image_id,
            "worker_image_id": worker_image_id,
            "codegen_revision": codegen_revision,
            "behavior_configuration_sha256": behavior_configuration_sha256,
            "egress_policy_sha256": egress_policy_sha256,
            "egress_proxy_image_id": egress_proxy_image_id,
            "egress_transport": "network_none_unix_socket@1",
            "max_concurrent_jobs": max_concurrent_jobs,
        }
        return cls.model_validate(
            {**payload, "identity_sha256": canonical_sha256(payload)}
        )

    @model_validator(mode="after")
    def validate_identity(self) -> TenantPublicationRuntimeIdentity:
        if self.codegen_revision != self.codegen_revision.strip():
            raise ValueError("runtime codegen_revision must be normalized")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"identity_sha256"})
        )
        if self.identity_sha256 != expected:
            raise ValueError("runtime identity_sha256 does not match its contents")
        return self


class TenantPublicationRequest(_StrictModel):
    """Exact tenant assignment and runtime authority for one draft PR."""

    schema_version: Literal["tenant_publication_request@1"]
    requested_stage: Literal[PublicationStage.tenant_draft_pr]
    risk: RiskLevel
    execution_snapshot: LlmExecutionSnapshot
    execution_snapshot_sha256: Sha256
    runtime_identity: TenantPublicationRuntimeIdentity

    @model_validator(mode="after")
    def bind_runtime_to_snapshot(self) -> TenantPublicationRequest:
        if self.execution_snapshot.rollout_stage != self.requested_stage.value:
            raise ValueError("publication stage does not match the execution snapshot")
        if self.execution_snapshot_sha256 != self.execution_snapshot.evidence_sha256():
            raise ValueError(
                "execution_snapshot_sha256 does not match the execution snapshot"
            )
        if (
            self.runtime_identity.codegen_revision
            != self.execution_snapshot.codegen_revision
        ):
            raise ValueError("runtime revision does not match the execution snapshot")
        if (
            self.runtime_identity.behavior_configuration_sha256
            != self.execution_snapshot.behavior_configuration_sha256
        ):
            raise ValueError(
                "runtime behavior configuration does not match the execution snapshot"
            )
        return self


class TenantPublicationDecision(_StrictModel):
    """Fixed draft-only capabilities granted by tenant model assignments."""

    schema_version: Literal["tenant_publication_decision@1"]
    requested_stage: Literal[PublicationStage.tenant_draft_pr]
    risk: RiskLevel
    allowed: Literal[True]
    publish_branch: Literal[True]
    create_pull_request: Literal[True]
    ready_for_review: Literal[False]
    reasons: tuple[()]
    decision_sha256: Sha256

    @model_validator(mode="after")
    def validate_digest(self) -> TenantPublicationDecision:
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"decision_sha256"})
        )
        if self.decision_sha256 != expected:
            raise ValueError("decision_sha256 does not match the tenant decision")
        return self


class TenantPublicationAuthorization(_StrictModel):
    """Digest-bound authority derived from exact tenant model assignments."""

    schema_version: Literal["tenant_publication_authorization@1"]
    authority: Literal["tenant_model_assignments"]
    request: TenantPublicationRequest
    decision: TenantPublicationDecision
    draft_only: Literal[True]
    authorization_sha256: Sha256

    @model_validator(mode="after")
    def validate_authorization(self) -> TenantPublicationAuthorization:
        if self.decision.requested_stage is not self.request.requested_stage:
            raise ValueError("tenant decision stage does not match its request")
        if self.decision.risk is not self.request.risk:
            raise ValueError("tenant decision risk does not match its request")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"authorization_sha256"})
        )
        if self.authorization_sha256 != expected:
            raise ValueError(
                "authorization_sha256 does not match the tenant authorization"
            )
        return self


class DevelopmentPublicationRequest(_StrictModel):
    """Local-development request for an always-draft PR."""

    schema_version: Literal["development_publication_request@1"]
    requested_stage: Literal[PublicationStage.development_pr]
    risk: RiskLevel
    model: str = Field(min_length=1)
    codegen_revision: Literal["local-development"]


class DevelopmentPublicationDecision(_StrictModel):
    """Fixed-capability decision for local development publication."""

    schema_version: Literal["development_publication_decision@1"]
    requested_stage: Literal[PublicationStage.development_pr]
    risk: RiskLevel
    allowed: Literal[True]
    publish_branch: Literal[True]
    create_pull_request: Literal[True]
    ready_for_review: Literal[False]
    reasons: tuple[()]
    decision_sha256: Sha256

    @model_validator(mode="after")
    def validate_digest(self) -> DevelopmentPublicationDecision:
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"decision_sha256"})
        )
        if self.decision_sha256 != expected:
            raise ValueError("decision_sha256 does not match the development decision")
        return self


class DevelopmentPublicationAuthorization(_StrictModel):
    """Auditable local-development authority with no fabricated evidence."""

    schema_version: Literal["development_publication_authorization@1"]
    authority: Literal["local_development"]
    request: DevelopmentPublicationRequest
    decision: DevelopmentPublicationDecision
    draft_only: Literal[True]
    authorization_sha256: Sha256

    @model_validator(mode="after")
    def validate_authorization(self) -> DevelopmentPublicationAuthorization:
        if self.decision.requested_stage is not self.request.requested_stage:
            raise ValueError("development decision stage does not match its request")
        if self.decision.risk is not self.request.risk:
            raise ValueError("development decision risk does not match its request")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"authorization_sha256"})
        )
        if self.authorization_sha256 != expected:
            raise ValueError(
                "authorization_sha256 does not match the development authorization"
            )
        return self


PublicationAuthorizationRecord = Annotated[
    TenantPublicationAuthorization | DevelopmentPublicationAuthorization,
    Field(discriminator="schema_version"),
]
PUBLICATION_AUTHORIZATION_ADAPTER = TypeAdapter(PublicationAuthorizationRecord)


def build_tenant_publication_authorization(
    *,
    risk: RiskLevel,
    snapshot: LlmExecutionSnapshot,
    runtime_identity: TenantPublicationRuntimeIdentity,
) -> TenantPublicationAuthorization:
    """Build a fixed draft authority bound to both tenant assignments."""
    request = TenantPublicationRequest(
        schema_version="tenant_publication_request@1",
        requested_stage=PublicationStage.tenant_draft_pr,
        risk=risk,
        execution_snapshot=snapshot,
        execution_snapshot_sha256=snapshot.evidence_sha256(),
        runtime_identity=runtime_identity,
    )
    decision_payload = {
        "schema_version": "tenant_publication_decision@1",
        "requested_stage": PublicationStage.tenant_draft_pr,
        "risk": risk,
        "allowed": True,
        "publish_branch": True,
        "create_pull_request": True,
        "ready_for_review": False,
        "reasons": (),
    }
    decision = TenantPublicationDecision(
        **decision_payload,
        decision_sha256=canonical_sha256(decision_payload),
    )
    payload = {
        "schema_version": "tenant_publication_authorization@1",
        "authority": "tenant_model_assignments",
        "request": request.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
        "draft_only": True,
    }
    return TenantPublicationAuthorization(
        schema_version="tenant_publication_authorization@1",
        authority="tenant_model_assignments",
        request=request,
        decision=decision,
        draft_only=True,
        authorization_sha256=canonical_sha256(payload),
    )


def build_development_publication_authorization(
    *,
    risk: RiskLevel,
    model: str,
    codegen_revision: str,
) -> DevelopmentPublicationAuthorization:
    """Build the fixed, digest-bound authority used only by local development."""
    request = DevelopmentPublicationRequest(
        schema_version="development_publication_request@1",
        requested_stage=PublicationStage.development_pr,
        risk=risk,
        model=model,
        codegen_revision=codegen_revision,
    )
    decision_payload = {
        "schema_version": "development_publication_decision@1",
        "requested_stage": PublicationStage.development_pr,
        "risk": risk,
        "allowed": True,
        "publish_branch": True,
        "create_pull_request": True,
        "ready_for_review": False,
        "reasons": (),
    }
    decision = DevelopmentPublicationDecision(
        **decision_payload,
        decision_sha256=canonical_sha256(decision_payload),
    )
    payload = {
        "schema_version": "development_publication_authorization@1",
        "authority": "local_development",
        "request": request.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
        "draft_only": True,
    }
    return DevelopmentPublicationAuthorization(
        schema_version="development_publication_authorization@1",
        authority="local_development",
        request=request,
        decision=decision,
        draft_only=True,
        authorization_sha256=canonical_sha256(payload),
    )


class PublicationGateError(RuntimeError):
    """Raised when this deployment has no trusted PR-publication capability."""


class PublicationGate(Protocol):
    """Minimal job dependency checked before any GitHub token is minted."""

    @property
    def stage(self) -> PublicationStage: ...

    def authorize(
        self,
        *,
        risk: RiskLevel,
        snapshot: LlmExecutionSnapshot,
    ) -> PublicationAuthorizationRecord: ...


@dataclass(frozen=True)
class ConfiguredPublicationGate:
    """Bind tenant assignment authority to the running deployment."""

    stage: PublicationStage
    codegen_revision: str
    behavior_configuration_sha256: str
    runtime_identity: TenantPublicationRuntimeIdentity | None = None
    development_mode: bool = False

    def __post_init__(self) -> None:
        if not self.codegen_revision.strip():
            raise ValueError("publication codegen revision cannot be empty")
        if len(self.behavior_configuration_sha256) != 64:
            raise ValueError("publication behavior configuration digest is invalid")
        tenant_stage = self.stage is PublicationStage.tenant_draft_pr
        if tenant_stage != (self.runtime_identity is not None):
            raise ValueError(
                "tenant_draft_pr requires one attested runtime identity; "
                "other stages must not receive one"
            )
        if self.runtime_identity is not None:
            if self.runtime_identity.codegen_revision != self.codegen_revision:
                raise ValueError("runtime identity revision does not match deployment")
            if (
                self.runtime_identity.behavior_configuration_sha256
                != self.behavior_configuration_sha256
            ):
                raise ValueError(
                    "runtime identity behavior does not match the deployment"
                )
        development_stage = self.stage is PublicationStage.development_pr
        if development_stage != self.development_mode:
            raise ValueError(
                "development_pr requires the explicit local development marker; "
                "other stages must not receive it"
            )
        if development_stage and self.codegen_revision != DEVELOPMENT_CODEGEN_REVISION:
            raise ValueError(
                "development_pr requires CODEGEN_REVISION=local-development"
            )

    def authorize(
        self,
        *,
        risk: RiskLevel,
        snapshot: LlmExecutionSnapshot,
    ) -> PublicationAuthorizationRecord:
        if self.stage is PublicationStage.offline:
            raise PublicationGateError(
                f"the {self.stage.value} publication stage cannot publish to GitHub"
            )
        snapshot = LlmExecutionSnapshot.model_validate(
            snapshot.model_dump(mode="python")
        )
        if snapshot.rollout_stage != self.stage.value:
            raise PublicationGateError(
                "execution snapshot does not match the configured publication stage"
            )
        if snapshot.codegen_revision != self.codegen_revision:
            raise PublicationGateError(
                "execution snapshot does not match the configured codegen revision"
            )
        if (
            snapshot.behavior_configuration_sha256
            != self.behavior_configuration_sha256
        ):
            raise PublicationGateError(
                "execution snapshot does not match the configured behavior"
            )
        if any(
            assignment.catalog_version != CATALOG_VERSION
            for assignment in snapshot.assignments
        ):
            raise PublicationGateError(
                "execution snapshot does not use the current provider catalog"
            )
        if self.stage is PublicationStage.development_pr:
            editor = snapshot.assignment("editor")
            return build_development_publication_authorization(
                risk=risk,
                model=runtime_model(editor.provider, editor.model_id).litellm_model,
                codegen_revision=self.codegen_revision,
            )
        if self.runtime_identity is None:  # guarded by __post_init__
            raise PublicationGateError("no attested tenant publication runtime exists")
        return build_tenant_publication_authorization(
            risk=risk,
            snapshot=snapshot,
            runtime_identity=self.runtime_identity,
        )
