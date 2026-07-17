"""Trusted publication authorization from operator-controlled evaluation evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from app.evaluations.metrics import aggregate_metrics
from app.evaluations.corpus import validate_run_provenance
from app.evaluations.json_io import parse_strict_json_object, read_bounded_regular_text
from app.evaluations.models import (
    EvaluationReport,
    RiskLevel,
    RolloutDecision,
    RolloutPolicy,
    RolloutStage,
    Sha256,
    StrictModel,
    canonical_sha256,
)
from app.evaluations.rollout import decide_rollout
from app.evaluations.segments import (
    SegmentedEvaluationReport,
    validate_segmented_report,
)


MAX_PUBLICATION_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_ROLLOUT_POLICY_BYTES = 64 * 1024


class PublicationEvidenceBundle(StrictModel):
    """Content-addressed report and policy selected by an operator."""

    schema_version: Literal["publication_evidence_bundle@5"] = (
        "publication_evidence_bundle@5"
    )
    report: EvaluationReport
    segmented_report: SegmentedEvaluationReport
    policy: RolloutPolicy
    candidate_identity_sha256: Sha256
    egress_policy_sha256: Sha256
    report_sha256: Sha256
    segmented_report_sha256: Sha256
    policy_sha256: Sha256
    bundle_sha256: Sha256

    @model_validator(mode="after")
    def validate_content_addresses(self) -> PublicationEvidenceBundle:
        candidate_identity = self.report.run.candidate_identity
        if candidate_identity is None:
            raise ValueError(
                "publication evidence requires a trusted candidate identity"
            )
        if self.candidate_identity_sha256 != candidate_identity.identity_sha256:
            raise ValueError(
                "bundle candidate identity does not match its evaluation run"
            )
        if self.egress_policy_sha256 != candidate_identity.egress_policy_sha256:
            raise ValueError(
                "bundle egress policy does not match its candidate identity"
            )
        if self.report_sha256 != self.report.report_sha256:
            raise ValueError("bundle report_sha256 does not match its report")
        if (
            self.segmented_report_sha256
            != self.segmented_report.segmented_report_sha256
        ):
            raise ValueError(
                "bundle segmented_report_sha256 does not match its segmented report"
            )
        validate_segmented_report(self.report, self.segmented_report)
        if self.policy_sha256 != canonical_sha256(self.policy):
            raise ValueError("bundle policy_sha256 does not match its policy")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"bundle_sha256"})
        )
        if self.bundle_sha256 != expected:
            raise ValueError("bundle_sha256 does not match the bundle contents")
        return self


class PublicationRequest(StrictModel):
    """Current generation identity and requested GitHub publication stage."""

    schema_version: Literal["publication_request@3"] = "publication_request@3"
    requested_stage: RolloutStage
    risk: RiskLevel
    model: str = Field(min_length=1)
    codegen_revision: str = Field(min_length=1)
    candidate_identity_sha256: Sha256
    egress_policy_sha256: Sha256
    canary_identity: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def publication_stage_only(self) -> PublicationRequest:
        if self.requested_stage not in {
            RolloutStage.reviewed_pr,
            RolloutStage.low_risk_canary,
        }:
            raise ValueError("publication requests must target a PR publication stage")
        if self.requested_stage is RolloutStage.low_risk_canary:
            if not self.canary_identity:
                raise ValueError("canary publication requires a stable identity")
        elif self.canary_identity is not None:
            raise ValueError("canary_identity is valid only for the canary stage")
        return self


class PublicationAuthorization(StrictModel):
    """Persistable proof that trusted evidence was evaluated for one request."""

    schema_version: Literal["publication_authorization@4"] = (
        "publication_authorization@4"
    )
    request: PublicationRequest
    expected_model: str = Field(min_length=1)
    expected_codegen_revision: str = Field(min_length=1)
    expected_candidate_identity_sha256: Sha256
    expected_egress_policy_sha256: Sha256
    report_sha256: Sha256
    segmented_report_sha256: Sha256
    bundle_sha256: Sha256
    policy_sha256: Sha256
    decision: RolloutDecision
    authorization_sha256: Sha256

    @model_validator(mode="after")
    def validate_authorization(self) -> PublicationAuthorization:
        if self.request.model != self.expected_model:
            raise ValueError("publication request model does not match expected_model")
        if self.request.codegen_revision != self.expected_codegen_revision:
            raise ValueError(
                "publication request revision does not match expected_codegen_revision"
            )
        if (
            self.request.candidate_identity_sha256
            != self.expected_candidate_identity_sha256
        ):
            raise ValueError(
                "publication request candidate identity does not match expected identity"
            )
        if (
            self.request.egress_policy_sha256
            != self.expected_egress_policy_sha256
        ):
            raise ValueError(
                "publication request egress policy does not match expected policy"
            )
        if self.decision.requested_stage is not self.request.requested_stage:
            raise ValueError("publication decision stage does not match its request")
        if self.decision.risk is not self.request.risk:
            raise ValueError("publication decision risk does not match its request")
        if self.decision.policy_sha256 != self.policy_sha256:
            raise ValueError("publication decision does not use the bundled policy")
        if self.decision.segmented_report_sha256 != self.segmented_report_sha256:
            raise ValueError(
                "publication decision does not use the bundled segmented report"
            )
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"authorization_sha256"})
        )
        if self.authorization_sha256 != expected:
            raise ValueError(
                "authorization_sha256 does not match the authorization contents"
            )
        return self


@runtime_checkable
class PublicationAuthorizationProvider(Protocol):
    """Small synchronous boundary for jobs that need a publication decision."""

    def authorize(self, request: PublicationRequest) -> PublicationAuthorization: ...


def _revalidate_bundle(bundle: PublicationEvidenceBundle) -> PublicationEvidenceBundle:
    payload = bundle.model_dump(mode="python")
    validated = PublicationEvidenceBundle.model_validate(payload)
    if validated.report.run.stage not in {RolloutStage.offline, RolloutStage.shadow}:
        raise ValueError(
            "publication evidence must come from a non-publishing evaluation run"
        )
    recomputed = aggregate_metrics(validated.report.run)
    if recomputed != validated.report.summary:
        raise ValueError("evaluation report summary does not match its run outcomes")
    validate_segmented_report(validated.report, validated.segmented_report)
    validate_run_provenance(validated.report.run)
    return validated


def build_publication_bundle(
    report: EvaluationReport,
    segmented_report: SegmentedEvaluationReport,
    policy: RolloutPolicy,
) -> PublicationEvidenceBundle:
    """Build an operator artifact after independently checking report arithmetic."""
    validated_report = EvaluationReport.model_validate(report.model_dump(mode="python"))
    validated_policy = RolloutPolicy.model_validate(policy.model_dump(mode="python"))
    if validated_report.run.stage not in {
        RolloutStage.offline,
        RolloutStage.shadow,
    }:
        raise ValueError(
            "publication evidence must come from a non-publishing evaluation run"
        )
    if aggregate_metrics(validated_report.run) != validated_report.summary:
        raise ValueError("evaluation report summary does not match its run outcomes")
    validate_run_provenance(validated_report.run)
    validated_segmented_report = validate_segmented_report(
        validated_report,
        segmented_report,
    )
    candidate_identity = validated_report.run.candidate_identity
    if candidate_identity is None:
        raise ValueError(
            "publication evidence requires the default trusted Docker candidate identity"
        )
    payload = {
        "schema_version": "publication_evidence_bundle@5",
        "report": validated_report.model_dump(mode="json"),
        "segmented_report": validated_segmented_report.model_dump(mode="json"),
        "policy": validated_policy.model_dump(mode="json"),
        "candidate_identity_sha256": candidate_identity.identity_sha256,
        "egress_policy_sha256": candidate_identity.egress_policy_sha256,
        "report_sha256": validated_report.report_sha256,
        "segmented_report_sha256": (validated_segmented_report.segmented_report_sha256),
        "policy_sha256": canonical_sha256(validated_policy),
    }
    return PublicationEvidenceBundle.model_validate_json(
        json.dumps(
            {**payload, "bundle_sha256": canonical_sha256(payload)},
            allow_nan=False,
            separators=(",", ":"),
        )
    )


def load_publication_bundle(
    path: Path,
    *,
    expected_model: str,
    expected_codegen_revision: str,
    expected_candidate_identity_sha256: str,
    expected_egress_policy_sha256: str,
) -> PublicationEvidenceBundle:
    """Strictly load and bind an operator-selected bundle to the active generator."""
    payload = parse_strict_json_object(
        read_bounded_regular_text(path, max_bytes=MAX_PUBLICATION_BUNDLE_BYTES)
    )
    bundle = _revalidate_bundle(
        PublicationEvidenceBundle.model_validate_json(
            json.dumps(payload, allow_nan=False, separators=(",", ":"))
        )
    )
    run = bundle.report.run
    if run.model != expected_model:
        raise ValueError(
            f"evaluation model {run.model!r} does not match expected {expected_model!r}"
        )
    if run.codegen_revision != expected_codegen_revision:
        raise ValueError(
            "evaluation codegen revision does not match the expected revision"
        )
    if bundle.candidate_identity_sha256 != expected_candidate_identity_sha256:
        raise ValueError(
            "evaluation candidate identity does not match the expected deployment"
        )
    if bundle.egress_policy_sha256 != expected_egress_policy_sha256:
        raise ValueError(
            "evaluation egress policy does not match the expected deployment"
        )
    return bundle


def load_rollout_policy(path: Path) -> RolloutPolicy:
    payload = parse_strict_json_object(
        read_bounded_regular_text(path, max_bytes=MAX_ROLLOUT_POLICY_BYTES)
    )
    return RolloutPolicy.model_validate_json(
        json.dumps(payload, allow_nan=False, separators=(",", ":"))
    )


class TrustedPublicationAuthorizer:
    """Immutable trusted snapshot that recomputes every request decision."""

    def __init__(
        self,
        bundle: PublicationEvidenceBundle,
        *,
        expected_model: str,
        expected_codegen_revision: str,
        expected_candidate_identity_sha256: str,
        expected_egress_policy_sha256: str,
    ) -> None:
        validated = _revalidate_bundle(bundle)
        if validated.report.run.model != expected_model:
            raise ValueError("evaluation model does not match expected_model")
        if validated.report.run.codegen_revision != expected_codegen_revision:
            raise ValueError(
                "evaluation codegen revision does not match expected_codegen_revision"
            )
        if validated.candidate_identity_sha256 != expected_candidate_identity_sha256:
            raise ValueError(
                "evaluation candidate identity does not match expected identity"
            )
        if validated.egress_policy_sha256 != expected_egress_policy_sha256:
            raise ValueError(
                "evaluation egress policy does not match expected policy"
            )
        self._bundle_json = validated.model_dump_json()
        self._expected_model = expected_model
        self._expected_codegen_revision = expected_codegen_revision
        self._expected_candidate_identity_sha256 = expected_candidate_identity_sha256
        self._expected_egress_policy_sha256 = expected_egress_policy_sha256

    @property
    def bundle_sha256(self) -> str:
        return self._snapshot().bundle_sha256

    def _snapshot(self) -> PublicationEvidenceBundle:
        payload = parse_strict_json_object(self._bundle_json)
        return _revalidate_bundle(
            PublicationEvidenceBundle.model_validate_json(
                json.dumps(payload, allow_nan=False, separators=(",", ":"))
            )
        )

    def authorize(self, request: PublicationRequest) -> PublicationAuthorization:
        """Revalidate current identity and derive a fresh decision from trusted inputs."""
        request = PublicationRequest.model_validate(request.model_dump(mode="python"))
        if request.model != self._expected_model:
            raise ValueError("publication request model does not match expected_model")
        if request.codegen_revision != self._expected_codegen_revision:
            raise ValueError(
                "publication request revision does not match expected_codegen_revision"
            )
        if (
            request.candidate_identity_sha256
            != self._expected_candidate_identity_sha256
        ):
            raise ValueError(
                "publication request candidate identity does not match expected identity"
            )
        if request.egress_policy_sha256 != self._expected_egress_policy_sha256:
            raise ValueError(
                "publication request egress policy does not match expected policy"
            )
        bundle = self._snapshot()
        decision = decide_rollout(
            requested_stage=request.requested_stage,
            risk=request.risk,
            summary=bundle.report.summary,
            segmented_report=bundle.segmented_report,
            policy=bundle.policy,
            canary_identity=request.canary_identity,
        )
        if (
            decision.evaluation_summary_sha256
            != bundle.report.summary.evidence_sha256()
        ):
            raise ValueError("publication decision does not bind the bundled summary")
        if (
            decision.segmented_report_sha256
            != bundle.segmented_report.segmented_report_sha256
        ):
            raise ValueError(
                "publication decision does not bind the bundled segmented report"
            )
        payload = {
            "schema_version": "publication_authorization@4",
            "request": request.model_dump(mode="json"),
            "expected_model": self._expected_model,
            "expected_codegen_revision": self._expected_codegen_revision,
            "expected_candidate_identity_sha256": (
                self._expected_candidate_identity_sha256
            ),
            "expected_egress_policy_sha256": self._expected_egress_policy_sha256,
            "report_sha256": bundle.report_sha256,
            "segmented_report_sha256": bundle.segmented_report_sha256,
            "bundle_sha256": bundle.bundle_sha256,
            "policy_sha256": bundle.policy_sha256,
            "decision": decision.model_dump(mode="json"),
        }
        return PublicationAuthorization.model_validate_json(
            json.dumps(
                {
                    **payload,
                    "authorization_sha256": canonical_sha256(payload),
                },
                allow_nan=False,
                separators=(",", ":"),
            )
        )


def load_publication_authorizer(
    path: Path,
    *,
    expected_model: str,
    expected_codegen_revision: str,
    expected_candidate_identity_sha256: str,
    expected_egress_policy_sha256: str,
) -> TrustedPublicationAuthorizer:
    bundle = load_publication_bundle(
        path,
        expected_model=expected_model,
        expected_codegen_revision=expected_codegen_revision,
        expected_candidate_identity_sha256=expected_candidate_identity_sha256,
        expected_egress_policy_sha256=expected_egress_policy_sha256,
    )
    return TrustedPublicationAuthorizer(
        bundle,
        expected_model=expected_model,
        expected_codegen_revision=expected_codegen_revision,
        expected_candidate_identity_sha256=expected_candidate_identity_sha256,
        expected_egress_policy_sha256=expected_egress_policy_sha256,
    )
