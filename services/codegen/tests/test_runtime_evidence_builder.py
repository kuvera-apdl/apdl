from datetime import UTC, datetime

import pytest

from app.models.observations import CIVerificationObservation, ExternalCIStatus
from app.runtime.collector import RuntimeCollectionDiagnostic, RuntimeEvidenceCollection
from app.runtime.evidence import build_runtime_evidence_observation
from app.runtime.models import RuntimeAcceptancePlan


def _plan() -> RuntimeAcceptancePlan:
    return RuntimeAcceptancePlan(
        source_ledger_sha256="a" * 64,
        repo_profile_sha256="b" * 64,
        verification_plan_sha256="c" * 64,
        repo="acme/widgets",
        branch="apdl/change",
    )


def _ci(head_sha: str, status: ExternalCIStatus) -> CIVerificationObservation:
    if status is not ExternalCIStatus.unverified_external_ci:
        raise ValueError("this focused helper only creates zero-signal CI")
    return CIVerificationObservation(
        observation_id="ciobs_" + "d" * 32,
        changeset_id="cs-1",
        repository="acme/widgets",
        pr_number=17,
        head_sha=head_sha,
        status=status,
        observed_at=datetime(2026, 7, 11, 11, 59, tzinfo=UTC),
    )


def test_runtime_observation_is_stable_and_copies_github_ci_without_promotion():
    collection = RuntimeEvidenceCollection(
        head_sha="head-a",
        diagnostics=[
            RuntimeCollectionDiagnostic(
                code="workflow_runs_missing",
                stage="workflow_runs",
                head_sha="head-a",
                message="No exact-head workflow run was observed.",
            )
        ],
    )
    first = build_runtime_evidence_observation(
        changeset_id="cs-1",
        repository="acme/widgets",
        pr_number=17,
        head_sha="head-a",
        ci_observation=_ci("head-a", ExternalCIStatus.unverified_external_ci),
        plan=_plan(),
        collection=collection,
        observed_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )
    second = build_runtime_evidence_observation(
        changeset_id="cs-1",
        repository="acme/widgets",
        pr_number=17,
        head_sha="head-a",
        ci_observation=_ci("head-a", ExternalCIStatus.unverified_external_ci),
        plan=_plan(),
        collection=collection,
        observed_at=datetime(2026, 7, 11, 12, 1, tzinfo=UTC),
    )

    assert first.observation_id == second.observation_id
    assert first.ci_observation_id == "ciobs_" + "d" * 32
    assert first.ci_evidence_hash == _ci(
        "head-a", ExternalCIStatus.unverified_external_ci
    ).evidence_hash()
    assert first.runtime_acceptance_plan_sha256 == _plan().evidence_hash()
    assert first.assessment.external_ci_status is ExternalCIStatus.unverified_external_ci
    assert first.collection_errors[0].startswith(
        "workflow_runs:workflow_runs_missing"
    )


def test_runtime_observation_rejects_a_stale_collection_head():
    with pytest.raises(ValueError, match="exact head"):
        build_runtime_evidence_observation(
            changeset_id="cs-1",
            repository="acme/widgets",
            pr_number=17,
            head_sha="head-new",
            ci_observation=_ci(
                "head-new", ExternalCIStatus.unverified_external_ci
            ),
            plan=_plan(),
            collection=RuntimeEvidenceCollection(head_sha="head-old"),
            observed_at=datetime.now(UTC),
        )


def test_runtime_observation_redacts_diagnostics_at_storage_boundary():
    secret = "Authorization: Bearer opaque-bearer-value"
    collection = RuntimeEvidenceCollection(
        head_sha="head-a",
        diagnostics=[
            RuntimeCollectionDiagnostic(
                code="actions_read_failed",
                stage="collector",
                head_sha="head-a",
                message=secret,
            )
        ],
    )

    observation = build_runtime_evidence_observation(
        changeset_id="cs-1",
        repository="acme/widgets",
        pr_number=17,
        head_sha="head-a",
        ci_observation=_ci("head-a", ExternalCIStatus.unverified_external_ci),
        plan=_plan(),
        collection=collection,
        observed_at=datetime.now(UTC),
    )

    assert secret not in observation.collection_errors[0]
    assert "[REDACTED]" in observation.collection_errors[0]
