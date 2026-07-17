"""Strict local-development publication authority contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.evaluations.models import (
    RiskLevel,
    RolloutDecision,
    RolloutStage,
    canonical_sha256,
)
from app.evaluations.publication import PublicationRequest
from app.evaluations.rollout import decide_rollout
from app.publication import (
    PUBLICATION_AUTHORIZATION_ADAPTER,
    DEVELOPMENT_CODEGEN_REVISION,
    DevelopmentPublicationAuthorization,
    build_development_publication_authorization,
)


def _authorization() -> DevelopmentPublicationAuthorization:
    return build_development_publication_authorization(
        risk=RiskLevel.medium,
        model="test-model@1",
        codegen_revision=DEVELOPMENT_CODEGEN_REVISION,
    )


def test_development_authorization_is_strict_draft_only_and_has_no_evidence_claims():
    authorization = _authorization()
    payload = authorization.model_dump(mode="json")

    assert set(payload) == {
        "schema_version",
        "authority",
        "request",
        "decision",
        "draft_only",
        "authorization_sha256",
    }
    assert payload["schema_version"] == "development_publication_authorization@1"
    assert payload["authority"] == "local_development"
    assert payload["draft_only"] is True
    assert payload["decision"]["allowed"] is True
    assert payload["decision"]["publish_branch"] is True
    assert payload["decision"]["create_pull_request"] is True
    assert payload["decision"]["ready_for_review"] is False
    assert payload["decision"]["reasons"] == []
    assert not ({"report_sha256", "bundle_sha256", "policy_sha256"} & set(payload))

    parsed = PUBLICATION_AUTHORIZATION_ADAPTER.validate_json(
        json.dumps(payload, allow_nan=False, separators=(",", ":"))
    )
    assert parsed == authorization


def test_development_authorization_rejects_tampering_and_unknown_fields():
    payload = _authorization().model_dump(mode="json")
    payload["authorization_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="authorization_sha256"):
        PUBLICATION_AUTHORIZATION_ADAPTER.validate_json(json.dumps(payload))

    payload = _authorization().model_dump(mode="json")
    payload["evaluation_report_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PUBLICATION_AUTHORIZATION_ADAPTER.validate_json(json.dumps(payload))

    payload = _authorization().model_dump(mode="json")
    payload["request"]["codegen_revision"] = "unevaluated-production"
    with pytest.raises(ValidationError, match="local-development"):
        PUBLICATION_AUTHORIZATION_ADAPTER.validate_json(json.dumps(payload))


def test_development_decision_cannot_be_promoted_to_ready_for_review():
    payload = _authorization().model_dump(mode="json")
    payload["decision"]["ready_for_review"] = True
    with pytest.raises(ValidationError, match="Input should be False"):
        PUBLICATION_AUTHORIZATION_ADAPTER.validate_json(json.dumps(payload))


def test_evaluated_publication_contracts_reject_development_stage():
    with pytest.raises(ValidationError, match="publication requests must target"):
        PublicationRequest(
            requested_stage=RolloutStage.development_pr,
            risk=RiskLevel.low,
            model="test-model@1",
            codegen_revision="test-revision",
            candidate_identity_sha256="a" * 64,
            egress_policy_sha256="b" * 64,
        )

    decision_payload = {
        "schema_version": "rollout_decision@3",
        "requested_stage": RolloutStage.development_pr,
        "risk": RiskLevel.low,
        "allowed": True,
        "publish_branch": True,
        "create_pull_request": True,
        "ready_for_review": False,
        "reasons": [],
        "evaluation_summary_sha256": "b" * 64,
        "segmented_report_sha256": "d" * 64,
        "policy_sha256": "c" * 64,
        "canary_identity_sha256": None,
        "canary_bucket": None,
    }
    with pytest.raises(ValidationError, match="separate development publication"):
        RolloutDecision(
            **decision_payload,
            decision_sha256=canonical_sha256(decision_payload),
        )

    with pytest.raises(ValueError, match="separate development publication"):
        decide_rollout(
            requested_stage=RolloutStage.development_pr,
            risk=RiskLevel.low,
            summary=None,
        )
