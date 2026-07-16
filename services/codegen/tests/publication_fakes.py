"""Strict publication-gate fixtures shared by job and repair tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.evaluations.models import (
    RiskLevel,
    RolloutDecision,
    RolloutStage,
    canonical_sha256,
)
from app.evaluations.publication import (
    PublicationAuthorization,
    PublicationRequest,
)


@dataclass
class FakePublicationGate:
    stage: RolloutStage = RolloutStage.low_risk_canary
    allowed: bool = True
    reasons: list[str] = field(default_factory=lambda: ["evaluation threshold failed"])
    calls: list[tuple[RiskLevel, str]] = field(default_factory=list)

    def authorize(
        self,
        *,
        risk: RiskLevel,
        canary_identity: str,
    ) -> PublicationAuthorization:
        self.calls.append((risk, canary_identity))
        request = PublicationRequest(
            requested_stage=self.stage,
            risk=risk,
            model="test-model@1",
            codegen_revision="test-revision",
            candidate_identity_sha256="a" * 64,
            canary_identity=(
                canary_identity
                if self.stage is RolloutStage.low_risk_canary
                else None
            ),
        )
        decision_payload = {
            "schema_version": "rollout_decision@2",
            "requested_stage": self.stage,
            "risk": risk,
            "allowed": self.allowed,
            "publish_branch": self.allowed,
            "create_pull_request": self.allowed,
            "ready_for_review": (
                self.allowed and self.stage is RolloutStage.low_risk_canary
            ),
            "reasons": [] if self.allowed else self.reasons,
            "evaluation_summary_sha256": "d" * 64,
            "policy_sha256": "c" * 64,
            "canary_identity_sha256": (
                "b" * 64
                if self.stage is RolloutStage.low_risk_canary
                else None
            ),
            "canary_bucket": (
                0 if self.stage is RolloutStage.low_risk_canary else None
            ),
        }
        decision = RolloutDecision(
            **decision_payload,
            decision_sha256=canonical_sha256(decision_payload),
        )
        authorization_payload = {
            "schema_version": "publication_authorization@2",
            "request": request.model_dump(mode="python"),
            "expected_model": "test-model@1",
            "expected_codegen_revision": "test-revision",
            "expected_candidate_identity_sha256": "a" * 64,
            "report_sha256": "e" * 64,
            "bundle_sha256": "f" * 64,
            "policy_sha256": "c" * 64,
            "decision": decision.model_dump(mode="python"),
        }
        return PublicationAuthorization(
            **authorization_payload,
            authorization_sha256=canonical_sha256(authorization_payload),
        )


def allowing_publication_gate(
    stage: RolloutStage = RolloutStage.low_risk_canary,
) -> FakePublicationGate:
    return FakePublicationGate(stage=stage)


def denying_publication_gate() -> FakePublicationGate:
    return FakePublicationGate(allowed=False)
