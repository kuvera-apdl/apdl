"""Exact-head GitHub CI observation construction and requirement mapping tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.github.observations import (
    CIObservationBuildError,
    StaleCIHeadError,
    build_ci_verification_observation,
    build_pull_request_observation,
)
from app.models.observations import (
    CISignalConclusion,
    CISignalKind,
    ExternalCIStatus,
    GitHubPRStatus,
    RequirementVerificationStatus,
)
from app.requirements.models import (
    GitHubCheckExpectation,
    ObservableAssertionExpectation,
    RepositoryCommandExpectation,
    Requirement,
    RequirementLedger,
    RequirementRisk,
    RequirementSourceKind,
)

NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)


def _requirement(
    number: int,
    expectation: GitHubCheckExpectation
    | RepositoryCommandExpectation
    | ObservableAssertionExpectation,
) -> Requirement:
    requirement_id = f"REQ-{number:03d}"
    return Requirement(
        requirement_id=requirement_id,
        source_kind=RequirementSourceKind.acceptance_criterion,
        original_source_text=f"Requirement {number}",
        observable_behavior=f"Behavior {number}",
        implementable_scope=f"Implement behavior {number}",
        expected_ci_evidence=[expectation],
        risk=RequirementRisk.medium,
    )


def _mixed_ledger() -> RequirementLedger:
    return RequirementLedger(
        title="Exact CI mapping",
        source_sha256="a" * 64,
        requirements=[
            _requirement(
                1,
                GitHubCheckExpectation(
                    evidence_id="CI-REQ-001-01",
                    check_name="api / pytest",
                    assertion="API tests pass.",
                ),
            ),
            _requirement(
                2,
                GitHubCheckExpectation(
                    evidence_id="CI-REQ-002-01",
                    check_name="API / pytest",
                    assertion="A differently-cased exact check passes.",
                ),
            ),
            _requirement(
                3,
                RepositoryCommandExpectation(
                    evidence_id="CI-REQ-003-01",
                    command="pytest -q",
                    cwd="services/api",
                    assertion="Repository command passes in GitHub CI.",
                ),
            ),
            _requirement(
                4,
                ObservableAssertionExpectation(
                    evidence_id="CI-REQ-004-01",
                    assertion="The endpoint responds successfully.",
                ),
            ),
            _requirement(
                5,
                GitHubCheckExpectation(
                    evidence_id="CI-REQ-005-01",
                    check_name="legacy / lint",
                    assertion="Commit-status lint passes.",
                ),
            ),
        ],
    )


def _build(
    *,
    combined_status: dict,
    check_runs: list[dict],
    ledger: RequirementLedger | None = None,
    observed_at: datetime = NOW,
    **budgets,
):
    return build_ci_verification_observation(
        changeset_id="cs-1",
        repository="acme/widgets",
        pr_number=17,
        head_sha="head-new",
        combined_status=combined_status,
        check_runs=check_runs,
        ledger=ledger,
        observed_at=observed_at,
        **budgets,
    )


def test_no_signals_are_unverified_and_never_synthesized_as_passed():
    ledger = _mixed_ledger()
    observation = _build(
        combined_status={"sha": "head-new", "state": "success", "statuses": []},
        check_runs=[],
        ledger=ledger,
    )

    assert observation.status is ExternalCIStatus.unverified_external_ci
    assert observation.signals == []
    assert all(
        result.status is RequirementVerificationStatus.unverified
        for result in observation.requirement_results
    )
    assert observation.failure_key is None
    assert observation.observation_id.startswith("ciobs_")


@pytest.mark.parametrize("combined_state", ["pending", "failure", "error"])
def test_combined_rollup_prevents_a_contradictory_signal_pass(
    combined_state: str,
):
    observation = _build(
        combined_status={
            "sha": "head-new",
            "state": combined_state,
            "statuses": [
                {
                    "sha": "head-new",
                    "context": "lint",
                    "state": "success",
                }
            ],
        },
        check_runs=[],
    )

    assert observation.status is ExternalCIStatus.pending
    assert observation.signals[0].conclusion is CISignalConclusion.passed


def test_only_observed_success_and_skipped_pass_with_exact_requirement_mapping():
    ledger = _mixed_ledger()
    statuses = [
        {
            "sha": "head-new",
            "context": "legacy / lint",
            "state": "success",
            "description": "lint passed",
            "target_url": "https://github.test/status/lint",
        }
    ]
    check_runs = [
        {
            "id": 11,
            "head_sha": "head-new",
            "name": "optional docs",
            "status": "completed",
            "conclusion": "skipped",
        },
        {
            "id": 10,
            "head_sha": "head-new",
            "name": "api / pytest",
            "status": "completed",
            "conclusion": "success",
            "check_suite": {"id": 3, "head_sha": "head-new"},
        },
    ]
    first = _build(
        combined_status={"sha": "head-new", "statuses": statuses},
        check_runs=check_runs,
        ledger=ledger,
    )
    second = _build(
        combined_status={"sha": "head-new", "statuses": list(reversed(statuses))},
        check_runs=list(reversed(check_runs)),
        ledger=ledger,
        observed_at=NOW + timedelta(minutes=1),
    )

    assert first.status is ExternalCIStatus.passed
    assert {signal.kind for signal in first.signals} == {
        CISignalKind.check_run,
        CISignalKind.commit_status,
    }
    results = {result.requirement_id: result for result in first.requirement_results}
    assert results["REQ-001"].status is RequirementVerificationStatus.passed
    assert results["REQ-001"].matched_signal_ids == ["check_run:10"]
    # Exact means case-sensitive; a similar name is not substituted.
    assert results["REQ-002"].status is RequirementVerificationStatus.unverified
    assert results["REQ-002"].matched_signal_ids == []
    assert results["REQ-003"].status is RequirementVerificationStatus.unverified
    assert results["REQ-004"].status is RequirementVerificationStatus.unverified
    assert results["REQ-005"].status is RequirementVerificationStatus.passed
    assert first.observation_id == second.observation_id
    assert first.evidence_hash() == second.evidence_hash()


def test_neutral_or_real_running_signals_remain_pending():
    ledger = _mixed_ledger()
    observation = _build(
        combined_status={"sha": "head-new", "statuses": []},
        check_runs=[
            {
                "id": 10,
                "head_sha": "head-new",
                "name": "api / pytest",
                "status": "completed",
                "conclusion": "neutral",
            },
            {
                "id": 12,
                "head_sha": "head-new",
                "name": "slow integration",
                "status": "in_progress",
                "conclusion": None,
            },
        ],
        ledger=ledger,
    )

    assert observation.status is ExternalCIStatus.pending
    assert {signal.conclusion for signal in observation.signals} == {
        CISignalConclusion.neutral,
        CISignalConclusion.pending,
    }
    results = {
        result.requirement_id: result for result in observation.requirement_results
    }
    assert results["REQ-001"].status is RequirementVerificationStatus.pending
    # Non-check expectations remain pending while real CI is running.
    assert results["REQ-003"].status is RequirementVerificationStatus.pending
    assert results["REQ-004"].status is RequirementVerificationStatus.pending


def test_skipped_only_signals_and_requirements_do_not_pass():
    observation = _build(
        combined_status={"sha": "head-new", "statuses": []},
        check_runs=[
            {
                "id": 10,
                "head_sha": "head-new",
                "name": "api / pytest",
                "status": "completed",
                "conclusion": "skipped",
            }
        ],
        ledger=_mixed_ledger(),
    )

    assert observation.status is ExternalCIStatus.pending
    result = next(
        item
        for item in observation.requirement_results
        if item.requirement_id == "REQ-001"
    )
    assert result.status is RequirementVerificationStatus.unverified


def test_any_failure_wins_and_failure_evidence_is_bounded_and_stable():
    annotations = [
        {
            "path": f"tests/test_{index}.py",
            "start_line": index + 1,
            "end_line": index + 1,
            "annotation_level": "failure",
            "message": "assertion failed " + "x" * 100,
        }
        for index in range(4)
    ]
    runs = [
        {
            "id": 20,
            "head_sha": "head-new",
            "name": "api / pytest",
            "status": "completed",
            "conclusion": "failure",
            "details_url": "https://github.test/runs/20",
            "output": {
                "title": "Test failure",
                "summary": "summary " + "s" * 200,
            },
            "_failure_annotations": annotations,
            "check_suite": {"id": 7, "head_sha": "head-new"},
        },
        {
            "id": 21,
            "head_sha": "head-new",
            "name": "still running",
            "status": "in_progress",
            "conclusion": None,
        },
    ]
    first = _build(
        combined_status={"sha": "head-new", "statuses": []},
        check_runs=runs,
        ledger=_mixed_ledger(),
        max_annotations_per_signal=2,
        max_signal_summary_chars=80,
        max_annotation_message_chars=40,
        max_failure_summary_chars=220,
    )
    second = _build(
        combined_status={"sha": "head-new", "statuses": []},
        check_runs=list(reversed(runs)),
        ledger=_mixed_ledger(),
        observed_at=NOW + timedelta(seconds=5),
        max_annotations_per_signal=2,
        max_signal_summary_chars=80,
        max_annotation_message_chars=40,
        max_failure_summary_chars=220,
    )

    assert first.status is ExternalCIStatus.failed
    failed = next(signal for signal in first.signals if signal.check_run_id == 20)
    assert len(failed.annotations) == 2
    assert len(failed.summary or "") <= 80
    assert all(len(item.message) <= 40 for item in failed.annotations)
    assert len(first.failure_summary or "") <= 220
    assert first.failure_key == second.failure_key
    assert first.observation_id == second.observation_id
    result = next(
        result
        for result in first.requirement_results
        if result.requirement_id == "REQ-001"
    )
    assert result.status is RequirementVerificationStatus.failed
    assert result.matched_signal_ids == ["check_run:20"]


def test_stale_heads_and_conflicting_duplicate_signals_are_rejected():
    with pytest.raises(StaleCIHeadError, match="combined status"):
        _build(
            combined_status={"sha": "head-old", "statuses": []},
            check_runs=[],
        )
    with pytest.raises(StaleCIHeadError, match="check run"):
        _build(
            combined_status={"sha": "head-new", "statuses": []},
            check_runs=[
                {
                    "id": 1,
                    "head_sha": "head-old",
                    "name": "tests",
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        )
    with pytest.raises(CIObservationBuildError, match="conflicting"):
        _build(
            combined_status={
                "sha": "head-new",
                "statuses": [
                    {"sha": "head-new", "context": "lint", "state": "success"},
                    {"sha": "head-new", "context": "lint", "state": "failure"},
                ],
            },
            check_runs=[],
        )


def test_missing_head_sha_is_rejected_for_every_github_signal_source():
    with pytest.raises(StaleCIHeadError, match="combined status.*missing"):
        _build(combined_status={"statuses": []}, check_runs=[])
    with pytest.raises(StaleCIHeadError, match="commit status.*missing"):
        _build(
            combined_status={
                "sha": "head-new",
                "statuses": [{"context": "lint", "state": "success"}],
            },
            check_runs=[],
        )
    with pytest.raises(StaleCIHeadError, match="check run.*missing"):
        _build(
            combined_status={"sha": "head-new", "statuses": []},
            check_runs=[
                {
                    "id": 1,
                    "name": "tests",
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        )


def test_pull_request_observation_uses_live_exact_head_and_github_timestamp():
    observation = build_pull_request_observation(
        changeset_id="cs-1",
        repository="acme/widgets",
        action="ready_for_review",
        delivery_id="delivery-1",
        observed_at=NOW,
        pull_request={
            "number": 17,
            "head": {"sha": "head-new"},
            "state": "open",
            "draft": False,
            "merged": False,
            "html_url": "https://github.com/acme/widgets/pull/17",
            "updated_at": "2026-07-11T11:59:00Z",
        },
    )

    assert observation.status is GitHubPRStatus.open
    assert observation.head_sha == "head-new"
    assert observation.delivery_id == "delivery-1"
    assert observation.github_updated_at < observation.observed_at


def test_pull_request_observation_rejects_missing_head_and_action_state_conflict():
    base = {
        "number": 17,
        "state": "open",
        "draft": True,
        "merged": False,
        "html_url": "https://github.com/acme/widgets/pull/17",
        "updated_at": "2026-07-11T11:59:00Z",
    }
    with pytest.raises(CIObservationBuildError, match="exact head"):
        build_pull_request_observation(
            changeset_id="cs-1",
            repository="acme/widgets",
            action="opened",
            observed_at=NOW,
            pull_request={**base, "head": {}},
        )
    with pytest.raises(ValueError, match="inconsistent"):
        build_pull_request_observation(
            changeset_id="cs-1",
            repository="acme/widgets",
            action="ready_for_review",
            observed_at=NOW,
            pull_request={**base, "head": {"sha": "head-new"}},
        )
