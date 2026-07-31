"""Strict tenant and local-development publication authority contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.llm.contracts import LlmAssignmentSnapshot, LlmExecutionSnapshot
from app.llm.provider_catalog import CATALOG_VERSION
from app.models.execution import PublicationStage, RiskLevel
from app.publication import (
    PUBLICATION_AUTHORIZATION_ADAPTER,
    DEVELOPMENT_CODEGEN_REVISION,
    DevelopmentPublicationAuthorization,
    TenantPublicationAuthorization,
    TenantPublicationRuntimeIdentity,
    build_development_publication_authorization,
    build_tenant_publication_authorization,
)


def _development_authorization() -> DevelopmentPublicationAuthorization:
    return build_development_publication_authorization(
        risk=RiskLevel.medium,
        model="test-model@1",
        codegen_revision=DEVELOPMENT_CODEGEN_REVISION,
    )


def _assignment(role: str) -> LlmAssignmentSnapshot:
    return LlmAssignmentSnapshot(
        schema_version="codegen_llm_assignment_snapshot@1",
        role=role,
        provider="anthropic",
        model_id="claude-sonnet-5",
        assignment_version=3 if role == "editor" else 4,
        connection_version=2,
        inventory_version=5,
        catalog_version=CATALOG_VERSION,
        context_window_tokens=200_000,
        supports_tool_calling=True,
        supports_structured_output=True,
        input_cost_per_million_tokens_usd_micros=3_000_000,
        output_cost_per_million_tokens_usd_micros=15_000_000,
    )


def _execution_snapshot(
    stage: PublicationStage = PublicationStage.tenant_draft_pr,
) -> LlmExecutionSnapshot:
    return LlmExecutionSnapshot(
        schema_version="codegen_llm_execution_snapshot@2",
        project_id="demo",
        repository_grant_id="ghg_demo",
        repository_id=10,
        repository_installation_id=20,
        repository_full_name="acme/widgets",
        codegen_revision="test-revision",
        behavior_configuration_sha256="a" * 64,
        rollout_stage=stage.value,
        assignments=(_assignment("editor"), _assignment("helper")),
    )


def _runtime_identity() -> TenantPublicationRuntimeIdentity:
    return TenantPublicationRuntimeIdentity.build(
        controller_image_id=f"sha256:{'1' * 64}",
        worker_image_id=f"sha256:{'2' * 64}",
        codegen_revision="test-revision",
        behavior_configuration_sha256="a" * 64,
        egress_policy_sha256="3" * 64,
        egress_proxy_image_id=f"sha256:{'4' * 64}",
        max_concurrent_jobs=1,
    )


def test_tenant_authorization_is_bound_to_exact_snapshot_and_always_draft():
    snapshot = _execution_snapshot()
    authorization = build_tenant_publication_authorization(
        risk=RiskLevel.low,
        snapshot=snapshot,
        runtime_identity=_runtime_identity(),
    )

    assert isinstance(authorization, TenantPublicationAuthorization)
    assert authorization.request.requested_stage is PublicationStage.tenant_draft_pr
    assert authorization.request.execution_snapshot == snapshot
    assert (
        authorization.request.execution_snapshot_sha256
        == snapshot.evidence_sha256()
    )
    assert authorization.decision.allowed is True
    assert authorization.decision.publish_branch is True
    assert authorization.decision.create_pull_request is True
    assert authorization.decision.ready_for_review is False
    assert authorization.draft_only is True


def test_development_authorization_is_strict_draft_only_and_has_no_evidence_claims():
    authorization = _development_authorization()
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
    payload = _development_authorization().model_dump(mode="json")
    payload["authorization_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="authorization_sha256"):
        PUBLICATION_AUTHORIZATION_ADAPTER.validate_json(json.dumps(payload))

    payload = _development_authorization().model_dump(mode="json")
    payload["evaluation_report_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PUBLICATION_AUTHORIZATION_ADAPTER.validate_json(json.dumps(payload))

    payload = _development_authorization().model_dump(mode="json")
    payload["request"]["codegen_revision"] = "unevaluated-production"
    with pytest.raises(ValidationError, match="local-development"):
        PUBLICATION_AUTHORIZATION_ADAPTER.validate_json(json.dumps(payload))


def test_development_decision_cannot_be_promoted_to_ready_for_review():
    payload = _development_authorization().model_dump(mode="json")
    payload["decision"]["ready_for_review"] = True
    with pytest.raises(ValidationError, match="Input should be False"):
        PUBLICATION_AUTHORIZATION_ADAPTER.validate_json(json.dumps(payload))


def test_tenant_publication_rejects_a_development_execution_snapshot():
    with pytest.raises(
        ValidationError,
        match="publication stage does not match the execution snapshot",
    ):
        build_tenant_publication_authorization(
            risk=RiskLevel.low,
            snapshot=_execution_snapshot(PublicationStage.development_pr),
            runtime_identity=_runtime_identity(),
        )


def test_llm_snapshots_require_explicit_schema_versions():
    assignment_payload = _assignment("editor").model_dump(mode="json")
    assignment_payload.pop("schema_version")
    with pytest.raises(ValidationError, match="schema_version"):
        LlmAssignmentSnapshot.model_validate(assignment_payload)

    snapshot_payload = _execution_snapshot().model_dump(mode="json")
    snapshot_payload.pop("schema_version")
    with pytest.raises(ValidationError, match="schema_version"):
        LlmExecutionSnapshot.model_validate(snapshot_payload)


def test_execution_snapshot_uses_canonical_repository_and_revision_bounds():
    payload = _execution_snapshot().model_dump(mode="python")
    payload["repository_grant_id"] = "ghg_" + "a" * 128
    assert len(payload["repository_grant_id"]) == 132
    LlmExecutionSnapshot.model_validate(payload)

    payload["repository_grant_id"] += "a"
    with pytest.raises(ValidationError, match="repository_grant_id"):
        LlmExecutionSnapshot.model_validate(payload)

    payload = _execution_snapshot().model_dump(mode="python")
    payload["codegen_revision"] = " test-revision"
    with pytest.raises(ValidationError, match="normalized"):
        LlmExecutionSnapshot.model_validate(payload)


@pytest.mark.parametrize(
    "path",
    [
        ("schema_version",),
        ("authority",),
        ("draft_only",),
        ("request", "schema_version"),
        ("request", "requested_stage"),
        ("request", "runtime_identity", "schema_version"),
        ("request", "runtime_identity", "egress_transport"),
        ("request", "runtime_identity", "max_concurrent_jobs"),
        ("decision", "schema_version"),
        ("decision", "requested_stage"),
        ("decision", "allowed"),
        ("decision", "publish_branch"),
        ("decision", "create_pull_request"),
        ("decision", "ready_for_review"),
        ("decision", "reasons"),
    ],
)
def test_tenant_authorization_rejects_missing_fixed_wire_fields(
    path: tuple[str, ...],
):
    payload = build_tenant_publication_authorization(
        risk=RiskLevel.low,
        snapshot=_execution_snapshot(),
        runtime_identity=_runtime_identity(),
    ).model_dump(mode="json")
    parent = payload
    for field in path[:-1]:
        parent = parent[field]
    parent.pop(path[-1])

    with pytest.raises(ValidationError, match=path[-1]):
        TenantPublicationAuthorization.model_validate(payload)


@pytest.mark.parametrize(
    "path",
    [
        ("schema_version",),
        ("authority",),
        ("draft_only",),
        ("request", "schema_version"),
        ("request", "requested_stage"),
        ("request", "codegen_revision"),
        ("decision", "schema_version"),
        ("decision", "requested_stage"),
        ("decision", "allowed"),
        ("decision", "publish_branch"),
        ("decision", "create_pull_request"),
        ("decision", "ready_for_review"),
        ("decision", "reasons"),
    ],
)
def test_development_authorization_rejects_missing_fixed_wire_fields(
    path: tuple[str, ...],
):
    payload = _development_authorization().model_dump(mode="json")
    parent = payload
    for field in path[:-1]:
        parent = parent[field]
    parent.pop(path[-1])

    with pytest.raises(ValidationError, match=path[-1]):
        DevelopmentPublicationAuthorization.model_validate(payload)
