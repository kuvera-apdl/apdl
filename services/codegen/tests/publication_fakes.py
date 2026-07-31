"""Strict publication-gate fixtures shared by job and repair tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.llm.contracts import LlmExecutionSnapshot
from app.llm.provider_catalog import runtime_model
from app.models.execution import PublicationStage, RiskLevel
from app.publication import (
    PublicationAuthorizationRecord,
    PublicationGateError,
    TenantPublicationRuntimeIdentity,
    build_development_publication_authorization,
    build_tenant_publication_authorization,
)


@dataclass
class FakePublicationGate:
    """Issue real draft-only authorizations for the exact supplied snapshot."""

    stage: PublicationStage = PublicationStage.tenant_draft_pr
    rejection: str | None = None
    calls: list[tuple[RiskLevel, LlmExecutionSnapshot]] = field(default_factory=list)

    def authorize(
        self,
        *,
        risk: RiskLevel,
        snapshot: LlmExecutionSnapshot,
    ) -> PublicationAuthorizationRecord:
        self.calls.append((risk, snapshot))
        if self.rejection is not None:
            raise PublicationGateError(self.rejection)
        if snapshot.rollout_stage != self.stage.value:
            raise PublicationGateError(
                "execution snapshot does not match the fake publication stage"
            )
        if self.stage is PublicationStage.development_pr:
            editor = snapshot.assignment("editor")
            return build_development_publication_authorization(
                risk=risk,
                model=runtime_model(
                    editor.provider,
                    editor.model_id,
                ).litellm_model,
                codegen_revision=snapshot.codegen_revision,
            )
        if self.stage is not PublicationStage.tenant_draft_pr:
            raise PublicationGateError(
                f"the {self.stage.value} publication stage cannot publish to GitHub"
            )
        runtime_identity = TenantPublicationRuntimeIdentity.build(
            controller_image_id=f"sha256:{'1' * 64}",
            worker_image_id=f"sha256:{'2' * 64}",
            codegen_revision=snapshot.codegen_revision,
            behavior_configuration_sha256=(
                snapshot.behavior_configuration_sha256
            ),
            egress_policy_sha256="3" * 64,
            egress_proxy_image_id=f"sha256:{'4' * 64}",
            max_concurrent_jobs=1,
        )
        return build_tenant_publication_authorization(
            risk=risk,
            snapshot=snapshot,
            runtime_identity=runtime_identity,
        )


def allowing_publication_gate(
    stage: PublicationStage = PublicationStage.tenant_draft_pr,
) -> FakePublicationGate:
    return FakePublicationGate(stage=stage)


def denying_publication_gate() -> FakePublicationGate:
    return FakePublicationGate(rejection="tenant publication is unavailable")
