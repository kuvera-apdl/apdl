"""Production publication gate at the GitHub write-credential boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.evaluations.models import RiskLevel, RolloutStage
from app.evaluations.publication import (
    PublicationAuthorization,
    PublicationAuthorizationProvider,
    PublicationRequest,
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
    ) -> PublicationAuthorization: ...


@dataclass(frozen=True)
class ConfiguredPublicationGate:
    """Bind one operator evidence snapshot to the running model and revision."""

    stage: RolloutStage
    model: str
    codegen_revision: str
    provider: PublicationAuthorizationProvider | None = None

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("publication model identity cannot be empty")
        if not self.codegen_revision.strip():
            raise ValueError("publication codegen revision cannot be empty")
        publication_stage = self.stage in {
            RolloutStage.reviewed_pr,
            RolloutStage.low_risk_canary,
        }
        if publication_stage != (self.provider is not None):
            raise ValueError(
                "PR rollout stages require one trusted publication provider; "
                "offline and shadow stages must not receive one"
            )

    def authorize(
        self,
        *,
        risk: RiskLevel,
        canary_identity: str,
    ) -> PublicationAuthorization:
        if self.stage in {RolloutStage.offline, RolloutStage.shadow}:
            raise PublicationGateError(
                f"the {self.stage.value} rollout stage cannot publish to GitHub"
            )
        if self.provider is None:  # guarded by __post_init__; fail closed anyway
            raise PublicationGateError("no trusted publication evidence is configured")
        request = PublicationRequest(
            requested_stage=self.stage,
            risk=risk,
            model=self.model,
            codegen_revision=self.codegen_revision,
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
