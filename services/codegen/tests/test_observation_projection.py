"""Atomic projection tests for immutable GitHub observations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.observations import (
    CISignal,
    CISignalConclusion,
    CISignalKind,
    CIVerificationObservation,
    ExternalCIStatus,
    GitHubPRStatus,
    PullRequestObservation,
)
from app.store import changesets as changeset_store
from app.store.observations import (
    apply_ci_verification_observation,
    apply_pull_request_observation,
    claim_failed_ci_observation,
    project_repair_result,
)
from tests.fakes import FakePool

NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)


def _seed(pool: FakePool, *, head_sha: str = "head-a") -> None:
    pool.add_connection("demo", "acme/widgets")
    pool.add_changeset(
        "cs-1",
        status="pr_open",
        branch="apdl/change",
        pr_number=17,
        head_sha=head_sha,
        github_pr_status="open",
        external_ci_status="pending",
    )


def _pr(
    observation_id: str,
    *,
    head_sha: str = "head-a",
    status: GitHubPRStatus = GitHubPRStatus.open,
    action: str = "polled",
    github_updated_at: datetime = NOW,
    observed_at: datetime = NOW,
    merge_sha: str | None = None,
) -> PullRequestObservation:
    return PullRequestObservation(
        observation_id=observation_id,
        changeset_id="cs-1",
        repository="acme/widgets",
        pr_number=17,
        head_sha=head_sha,
        status=status,
        action=action,
        github_url="https://github.com/acme/widgets/pull/17",
        merge_sha=merge_sha,
        github_updated_at=github_updated_at,
        observed_at=observed_at,
    )


def _ci(
    observation_id: str,
    *,
    head_sha: str = "head-a",
    status: ExternalCIStatus,
    observed_at: datetime = NOW,
) -> CIVerificationObservation:
    signals: list[CISignal] = []
    failure_key = None
    failure_summary = None
    if status in {ExternalCIStatus.passed, ExternalCIStatus.failed}:
        conclusion = (
            CISignalConclusion.passed
            if status is ExternalCIStatus.passed
            else CISignalConclusion.failed
        )
        signals = [
            CISignal(
                signal_id="check_run:7",
                kind=CISignalKind.check_run,
                name="tests",
                conclusion=conclusion,
                check_suite_id=3,
                check_run_id=7,
            )
        ]
    if status is ExternalCIStatus.failed:
        failure_key = f"{head_sha}:check_suite:3"
        failure_summary = "tests failed"
    return CIVerificationObservation(
        observation_id=observation_id,
        changeset_id="cs-1",
        repository="acme/widgets",
        pr_number=17,
        head_sha=head_sha,
        status=status,
        signals=signals,
        observed_at=observed_at,
        failure_key=failure_key,
        failure_summary=failure_summary,
    )


@pytest.mark.asyncio
async def test_out_of_order_pr_observation_is_journaled_without_regressing_projection():
    pool = FakePool()
    _seed(pool)
    current = _pr(
        "pr-current",
        head_sha="head-b",
        github_updated_at=NOW + timedelta(minutes=2),
        observed_at=NOW + timedelta(minutes=2),
    )
    stale = _pr(
        "pr-stale",
        head_sha="head-a",
        github_updated_at=NOW + timedelta(minutes=1),
        observed_at=NOW + timedelta(minutes=3),
    )

    assert (await apply_pull_request_observation(pool, current)).projected is True
    result = await apply_pull_request_observation(pool, stale)

    assert result.inserted is True
    assert result.projected is False
    assert result.reason == "superseded_observation"
    changeset = await changeset_store.get_changeset(pool, "cs-1")
    assert changeset is not None
    assert changeset.head_sha == "head-b"
    assert len(pool.store["pull_request_observations"]) == 2


@pytest.mark.asyncio
async def test_github_pr_observations_alone_drive_close_merge_and_reopen_lifecycle():
    pool = FakePool()
    _seed(pool)
    merged = _pr(
        "pr-merged",
        status=GitHubPRStatus.merged,
        action="closed",
        merge_sha="merge-sha",
    )

    assert (await apply_pull_request_observation(pool, merged)).projected is True
    changeset = await changeset_store.get_changeset(pool, "cs-1")
    assert changeset is not None
    assert changeset.status.value == "merged"
    assert changeset.merge_sha == "merge-sha"

    pool = FakePool()
    _seed(pool)
    closed = _pr("pr-closed", status=GitHubPRStatus.closed, action="closed")
    reopened = _pr(
        "pr-reopened",
        status=GitHubPRStatus.open,
        action="reopened",
        github_updated_at=NOW + timedelta(minutes=1),
        observed_at=NOW + timedelta(minutes=1),
    )
    await apply_pull_request_observation(pool, closed)
    assert (await apply_pull_request_observation(pool, reopened)).projected is True
    changeset = await changeset_store.get_changeset(pool, "cs-1")
    assert changeset is not None
    assert changeset.status.value == "pr_open"
    assert changeset.github_pr_status is GitHubPRStatus.open


@pytest.mark.asyncio
async def test_ci_projection_never_changes_lifecycle_and_rejects_stale_head():
    pool = FakePool()
    _seed(pool)
    no_ci = _ci(
        "ci-none", status=ExternalCIStatus.unverified_external_ci
    )
    failed = _ci(
        "ci-failed",
        status=ExternalCIStatus.failed,
        observed_at=NOW + timedelta(minutes=1),
    )
    stale = _ci(
        "ci-stale",
        head_sha="head-old",
        status=ExternalCIStatus.passed,
        observed_at=NOW + timedelta(minutes=2),
    )

    await apply_ci_verification_observation(pool, no_ci)
    await apply_ci_verification_observation(pool, failed)
    stale_result = await apply_ci_verification_observation(pool, stale)

    assert stale_result.inserted is True
    assert stale_result.projected is False
    changeset = await changeset_store.get_changeset(pool, "cs-1")
    assert changeset is not None
    assert changeset.status.value == "pr_open"
    assert changeset.external_ci_status is ExternalCIStatus.failed
    assert changeset.ci_failure_summary == "tests failed"


@pytest.mark.asyncio
async def test_repair_claim_requires_latest_persisted_failure_and_result_is_head_cas():
    pool = FakePool()
    _seed(pool)
    failed = _ci(
        "ci-failed",
        status=ExternalCIStatus.failed,
        observed_at=datetime.now(UTC),
    )
    await apply_ci_verification_observation(pool, failed)

    claim = await claim_failed_ci_observation(
        pool,
        failed,
        claim_scope="check_suite:3",
        max_attempts=2,
        budget_seconds=3600,
    )
    duplicate = await claim_failed_ci_observation(
        pool,
        failed,
        claim_scope="check_suite:3",
        max_attempts=2,
        budget_seconds=3600,
    )

    assert claim.claimed is True
    assert claim.attempt_number == 1
    assert duplicate.claimed is False
    assert duplicate.reason == "duplicate_claim"
    assert (
        await project_repair_result(
            pool,
            changeset_id="cs-1",
            failed_head_sha="stale-head",
            resulting_head_sha="head-b",
            exhausted=False,
            error=None,
        )
        is False
    )
    assert (
        await project_repair_result(
            pool,
            changeset_id="cs-1",
            failed_head_sha="head-a",
            resulting_head_sha="head-b",
            exhausted=False,
            error=None,
        )
        is True
    )
    changeset = await changeset_store.get_changeset(pool, "cs-1")
    assert changeset is not None
    assert changeset.head_sha == "head-b"
    assert changeset.status.value == "pr_open"
    assert changeset.external_ci_status is ExternalCIStatus.pending
    assert changeset.ci_remediation_status.value == "awaiting_ci"
