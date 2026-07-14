"""Publication gates at the GitHub write-credential boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import Field, TypeAdapter, model_validator

from app.evaluations.models import (
    RiskLevel,
    RolloutStage,
    Sha256,
    StrictModel,
    canonical_sha256,
)
from app.evaluations.publication import (
    PublicationAuthorization,
    PublicationAuthorizationProvider,
    PublicationRequest,
)


DEVELOPMENT_CODEGEN_REVISION = "local-development"


class DevelopmentPublicationRequest(StrictModel):
    """Unevaluated local-development request for an always-draft PR."""

    schema_version: Literal["development_publication_request@1"] = (
        "development_publication_request@1"
    )
    requested_stage: Literal[RolloutStage.development_pr] = (
        RolloutStage.development_pr
    )
    risk: RiskLevel
    model: str = Field(min_length=1)
    codegen_revision: Literal["local-development"] = DEVELOPMENT_CODEGEN_REVISION


class DevelopmentPublicationDecision(StrictModel):
    """Fixed-capability decision for local development publication.

    Development may push a branch and open a draft pull request. It can never
    claim evaluation evidence or mark the pull request ready for review.
    """

    schema_version: Literal["development_publication_decision@1"] = (
        "development_publication_decision@1"
    )
    requested_stage: Literal[RolloutStage.development_pr] = (
        RolloutStage.development_pr
    )
    risk: RiskLevel
    allowed: Literal[True] = True
    publish_branch: Literal[True] = True
    create_pull_request: Literal[True] = True
    ready_for_review: Literal[False] = False
    reasons: list[str] = Field(default_factory=list, max_length=0)
    decision_sha256: Sha256

    @model_validator(mode="after")
    def validate_digest(self) -> DevelopmentPublicationDecision:
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"decision_sha256"})
        )
        if self.decision_sha256 != expected:
            raise ValueError("decision_sha256 does not match the development decision")
        return self


class DevelopmentPublicationAuthorization(StrictModel):
    """Auditable local-development authority with no fabricated evidence."""

    schema_version: Literal["development_publication_authorization@1"] = (
        "development_publication_authorization@1"
    )
    authority: Literal["local_development"] = "local_development"
    request: DevelopmentPublicationRequest
    decision: DevelopmentPublicationDecision
    draft_only: Literal[True] = True
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
    PublicationAuthorization | DevelopmentPublicationAuthorization,
    Field(discriminator="schema_version"),
]
PUBLICATION_AUTHORIZATION_ADAPTER = TypeAdapter(PublicationAuthorizationRecord)


def build_development_publication_authorization(
    *,
    risk: RiskLevel,
    model: str,
    codegen_revision: str,
) -> DevelopmentPublicationAuthorization:
    """Build the fixed, digest-bound authority used only by local development."""
    request = DevelopmentPublicationRequest(
        risk=risk,
        model=model,
        codegen_revision=codegen_revision,
    )
    decision_payload = {
        "schema_version": "development_publication_decision@1",
        "requested_stage": RolloutStage.development_pr,
        "risk": risk,
        "allowed": True,
        "publish_branch": True,
        "create_pull_request": True,
        "ready_for_review": False,
        "reasons": [],
    }
    decision = DevelopmentPublicationDecision(
        **decision_payload,
        decision_sha256=canonical_sha256(decision_payload),
    )
    authorization_payload = {
        "schema_version": "development_publication_authorization@1",
        "authority": "local_development",
        "request": request.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
        "draft_only": True,
    }
    return DevelopmentPublicationAuthorization(
        authority="local_development",
        request=request,
        decision=decision,
        draft_only=True,
        authorization_sha256=canonical_sha256(authorization_payload),
    )


class PublicationGateError(RuntimeError):
    """Raised when this deployment has no trusted PR-publication capability."""


class PublicationGate(Protocol):
    """Minimal job dependency checked before any GitHub token is minted."""

    @property
    def stage(self) -> RolloutStage: ...

    def authorize(
        self,
        *,
        risk: RiskLevel,
        canary_identity: str,
    ) -> PublicationAuthorizationRecord: ...


@dataclass(frozen=True)
class ConfiguredPublicationGate:
    """Bind the selected publication authority to the running configuration."""

    stage: RolloutStage
    model: str
    codegen_revision: str
    candidate_identity_sha256: str | None = None
    provider: PublicationAuthorizationProvider | None = None
    development_mode: bool = False

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("publication model identity cannot be empty")
        if not self.codegen_revision.strip():
            raise ValueError("publication codegen revision cannot be empty")
        evaluated_publication_stage = self.stage in {
            RolloutStage.reviewed_pr,
            RolloutStage.low_risk_canary,
        }
        development_publication_stage = self.stage is RolloutStage.development_pr
        if evaluated_publication_stage != (self.provider is not None):
            raise ValueError(
                "evaluated PR rollout stages require one trusted publication "
                "provider; other stages must not receive one"
            )
        if evaluated_publication_stage != (
            self.candidate_identity_sha256 is not None
        ):
            raise ValueError(
                "evaluated PR rollout stages require the evaluated candidate "
                "identity; other stages must not receive one"
            )
        if development_publication_stage != self.development_mode:
            raise ValueError(
                "development_pr requires the explicit local development marker; "
                "other stages must not receive it"
            )
        if development_publication_stage and (
            self.codegen_revision != DEVELOPMENT_CODEGEN_REVISION
        ):
            raise ValueError(
                "development_pr requires CODEGEN_REVISION=local-development"
            )

    def authorize(
        self,
        *,
        risk: RiskLevel,
        canary_identity: str,
    ) -> PublicationAuthorizationRecord:
        if self.stage in {RolloutStage.offline, RolloutStage.shadow}:
            raise PublicationGateError(
                f"the {self.stage.value} rollout stage cannot publish to GitHub"
            )
        if self.stage is RolloutStage.development_pr:
            return build_development_publication_authorization(
                risk=risk,
                model=self.model,
                codegen_revision=self.codegen_revision,
            )
        if self.provider is None:  # guarded by __post_init__; fail closed anyway
            raise PublicationGateError("no trusted publication evidence is configured")
        if self.candidate_identity_sha256 is None:  # guarded by __post_init__
            raise PublicationGateError("no evaluated candidate identity is configured")
        request = PublicationRequest(
            requested_stage=self.stage,
            risk=risk,
            model=self.model,
            codegen_revision=self.codegen_revision,
            candidate_identity_sha256=self.candidate_identity_sha256,
            canary_identity=(
                canary_identity
                if self.stage is RolloutStage.low_risk_canary
                else None
            ),
        )
        authorization = PublicationAuthorization.model_validate(
            self.provider.authorize(request).model_dump(mode="python")
        )
        if authorization.request != request:
            raise PublicationGateError(
                "publication provider returned authorization for another request"
            )
        return authorization
