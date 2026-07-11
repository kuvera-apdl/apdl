"""Strict immutable GitHub observation and remediation contracts."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.models.observations import (
    CIRemediationAttempt,
    CISignalConclusion,
    CISignalKind,
    CISignal,
    CIVerificationObservation,
    ExternalCIStatus,
    FailureClassification,
    GitHubPRStatus,
    PullRequestObservation,
    RemediationDisposition,
    RequirementCIResult,
    RequirementVerificationStatus,
)

NOW = datetime.now(UTC)


def _check(
    signal_id: int = 7,
    conclusion: CISignalConclusion = CISignalConclusion.passed,
) -> CISignal:
    return CISignal(
        signal_id=f"check_run:{signal_id}",
        kind=CISignalKind.check_run,
        name="tests",
        conclusion=conclusion,
        check_suite_id=2,
        check_run_id=signal_id,
        github_url="https://github.com/acme/repo/actions/runs/1",
    )


def test_no_signal_can_only_be_unverified_or_pending():
    observation = CIVerificationObservation(
        observation_id="obs-1",
        changeset_id="cs-1",
        repository="acme/repo",
        pr_number=1,
        head_sha="abc",
        status=ExternalCIStatus.unverified_external_ci,
        observed_at=NOW,
    )
    assert observation.status is ExternalCIStatus.unverified_external_ci
    with pytest.raises(ValidationError, match="cannot pass without"):
        CIVerificationObservation(
            observation_id="obs-2",
            changeset_id="cs-1",
            repository="acme/repo",
            pr_number=1,
            head_sha="abc",
            status=ExternalCIStatus.passed,
            observed_at=NOW,
        )


def test_passed_observation_rejects_neutral_or_failed_signals():
    with pytest.raises(ValidationError, match="passed CI"):
        CIVerificationObservation(
            observation_id="obs-1",
            changeset_id="cs-1",
            repository="acme/repo",
            pr_number=1,
            head_sha="abc",
            status=ExternalCIStatus.passed,
            signals=[_check(conclusion=CISignalConclusion.neutral)],
            observed_at=NOW,
        )


def test_passed_observation_rejects_skipped_only_signals():
    with pytest.raises(ValidationError, match="only skipped"):
        CIVerificationObservation(
            observation_id="obs-skipped",
            changeset_id="cs-1",
            repository="acme/repo",
            pr_number=1,
            head_sha="abc",
            status=ExternalCIStatus.passed,
            signals=[_check(conclusion=CISignalConclusion.skipped)],
            observed_at=NOW,
        )


def test_requirement_results_reference_exact_known_signals():
    result = RequirementCIResult(
        requirement_id="REQ-001",
        evidence_id="CI-REQ-001-01",
        status=RequirementVerificationStatus.passed,
        matched_signal_ids=["check_run:7"],
        explanation="The exact expected test check passed.",
    )
    observation = CIVerificationObservation(
        observation_id="obs-1",
        changeset_id="cs-1",
        repository="acme/repo",
        pr_number=1,
        head_sha="abc",
        status=ExternalCIStatus.passed,
        signals=[_check()],
        requirement_results=[result],
        observed_at=NOW,
    )
    assert len(observation.requirement_results) == 1
    with pytest.raises(ValidationError, match="unknown CI signal"):
        payload = observation.model_dump()
        payload["requirement_results"][0]["matched_signal_ids"] = ["check_run:99"]
        CIVerificationObservation.model_validate(payload)


def test_observation_hash_ignores_record_identity_and_timestamp():
    first = CIVerificationObservation(
        observation_id="obs-1",
        changeset_id="cs-1",
        repository="acme/repo",
        pr_number=1,
        head_sha="abc",
        status=ExternalCIStatus.passed,
        signals=[_check()],
        observed_at=NOW,
    )
    second = first.model_copy(
        update={"observation_id": "obs-2", "observed_at": NOW.replace(microsecond=1)}
    )
    assert first.evidence_hash() == second.evidence_hash()


def test_merged_pull_request_requires_merge_sha():
    with pytest.raises(ValidationError, match="merge_sha"):
        PullRequestObservation(
            observation_id="pr-1",
            changeset_id="cs-1",
            repository="acme/repo",
            pr_number=1,
            head_sha="abc",
            status=GitHubPRStatus.merged,
            action="closed",
            github_url="https://github.com/acme/repo/pull/1",
            github_updated_at=NOW,
            observed_at=NOW,
        )


def test_pull_request_action_must_match_status():
    with pytest.raises(ValidationError, match="inconsistent"):
        PullRequestObservation(
            observation_id="pr-1",
            changeset_id="cs-1",
            repository="acme/repo",
            pr_number=1,
            head_sha="abc",
            status=GitHubPRStatus.draft,
            action="ready_for_review",
            github_url="https://github.com/acme/repo/pull/1",
            github_updated_at=NOW,
            observed_at=NOW,
        )


def test_observation_timestamps_must_be_timezone_aware():
    with pytest.raises(ValidationError, match="timezone-aware"):
        PullRequestObservation(
            observation_id="pr-1",
            changeset_id="cs-1",
            repository="acme/repo",
            pr_number=1,
            head_sha="abc",
            status=GitHubPRStatus.open,
            action="opened",
            github_url="https://github.com/acme/repo/pull/1",
            github_updated_at=NOW,
            observed_at=datetime(2026, 7, 11, 12, 0),
        )


def test_rerun_attempt_cannot_claim_code_changes():
    with pytest.raises(ValidationError, match="rerun"):
        CIRemediationAttempt(
            attempt_id="attempt-1",
            event_sequence=1,
            event_id="attempt-1:1",
            changeset_id="cs-1",
            repository="acme/repo",
            pr_number=1,
            failed_head_sha="abc",
            failure_observation_id="obs-1",
            attempt_number=1,
            classification=FailureClassification.flaky,
            confidence=0.8,
            changed_files=["app.py"],
            disposition=RemediationDisposition.rerun_requested,
            started_at=NOW,
            recorded_at=NOW,
            finished_at=NOW,
        )


def test_remediation_runtime_evidence_id_and_hash_are_strictly_paired():
    payload = {
        "attempt_id": "attempt-1",
        "event_sequence": 1,
        "event_id": "attempt-1:1",
        "changeset_id": "cs-1",
        "repository": "acme/repo",
        "pr_number": 1,
        "failed_head_sha": "abc",
        "failure_observation_id": "obs-1",
        "attempt_number": 1,
        "classification": FailureClassification.actionable_code,
        "confidence": 0.9,
        "disposition": RemediationDisposition.diagnosing,
        "started_at": NOW,
        "recorded_at": NOW,
    }
    with pytest.raises(ValidationError, match="recorded together"):
        CIRemediationAttempt.model_validate(
            {
                **payload,
                "runtime_evidence_observation_id": "runtime_obs_" + "a" * 32,
            }
        )
    with pytest.raises(ValidationError, match="recorded together"):
        CIRemediationAttempt.model_validate(
            {**payload, "runtime_evidence_hash": "b" * 64}
        )

    attempt = CIRemediationAttempt.model_validate(
        {
            **payload,
            "runtime_evidence_observation_id": "runtime_obs_" + "a" * 32,
            "runtime_evidence_hash": "b" * 64,
        }
    )
    assert attempt.runtime_evidence_hash == "b" * 64


def test_remediation_outcomes_are_new_immutable_events():
    awaiting = CIRemediationAttempt(
        attempt_id="attempt-1",
        event_sequence=1,
        event_id="attempt-1:1",
        changeset_id="cs-1",
        repository="acme/repo",
        pr_number=1,
        failed_head_sha="abc",
        failure_observation_id="obs-1",
        attempt_number=1,
        classification=FailureClassification.actionable_code,
        confidence=0.9,
        changed_files=["app.py"],
        resulting_commit_sha="def",
        disposition=RemediationDisposition.awaiting_ci,
        started_at=NOW,
        recorded_at=NOW,
    )
    finished = NOW + timedelta(minutes=1)
    repaired = awaiting.model_copy(
        update={
            "event_sequence": 2,
            "event_id": "attempt-1:2",
            "disposition": RemediationDisposition.repaired,
            "recorded_at": finished,
            "finished_at": finished,
        }
    )

    assert awaiting.event_id != repaired.event_id
    assert awaiting.attempt_id == repaired.attempt_id
    assert awaiting.finished_at is None
    assert repaired.finished_at == finished

    with pytest.raises(ValidationError, match="event_id"):
        CIRemediationAttempt.model_validate(
            {**awaiting.model_dump(), "event_id": "unrelated-event"}
        )


def test_failed_observation_groups_repair_claims_by_check_suite():
    observation = CIVerificationObservation(
        observation_id="obs-failed",
        changeset_id="cs-1",
        repository="acme/repo",
        pr_number=1,
        head_sha="abc",
        status=ExternalCIStatus.failed,
        signals=[
            _check(signal_id=7, conclusion=CISignalConclusion.failed),
            _check(signal_id=8, conclusion=CISignalConclusion.failed),
        ],
        observed_at=NOW,
        failure_key="failure",
        failure_summary="tests failed",
    )

    assert observation.remediation_claim_scopes() == ("check_suite:2",)


def test_contracts_reject_unknown_fields():
    with pytest.raises(ValidationError, match="Extra inputs"):
        CISignal(
            signal_id="check_run:7",
            kind=CISignalKind.check_run,
            name="tests",
            conclusion=CISignalConclusion.passed,
            check_run_id=7,
            surprise=True,
        )
