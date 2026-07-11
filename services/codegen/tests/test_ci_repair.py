"""Bounded, immutable CI repair on the exact GitHub pull-request head."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.editor.base import EditRequest, EditResult
from app.editor.fake import FakeEditor
from app.jobs.repair import repair_failed_ci
from app.models.changeset import ChangesetStatus
from app.models.observations import (
    CIRemediationStatus,
    CISignal,
    CISignalConclusion,
    CISignalKind,
    CIVerificationObservation,
    ExternalCIStatus,
    GitHubPRStatus,
    PullRequestObservation,
    RemediationDisposition,
)
from app.store import changesets as changeset_store
from app.store.observations import (
    apply_ci_verification_observation,
    apply_pull_request_observation,
    list_ci_remediation_attempts,
)
from tests.fakes import FakePool


async def _mint(_installation_id: int, _repo: str) -> str:
    return "ghs_tok"


def _failed_observation(
    *,
    observation_id: str = "ci-failed-1",
    head_sha: str = "head-failed",
    check_run_id: int = 101,
    check_suite_id: int = 11,
    name: str = "tests / pytest",
    summary: str = "tests: assertion failed",
) -> CIVerificationObservation:
    return CIVerificationObservation(
        observation_id=observation_id,
        changeset_id="cs-repair",
        repository="acme/widgets",
        pr_number=7,
        head_sha=head_sha,
        status=ExternalCIStatus.failed,
        signals=[
            CISignal(
                signal_id=f"check_run:{check_run_id}",
                kind=CISignalKind.check_run,
                name=name,
                conclusion=CISignalConclusion.failed,
                check_suite_id=check_suite_id,
                check_run_id=check_run_id,
                summary=summary,
            )
        ],
        observed_at=datetime.now(timezone.utc),
        failure_key=f"{head_sha}:check_suite:{check_suite_id}",
        failure_summary=summary,
    )


async def _seed_failed(
    *,
    observation: CIVerificationObservation | None = None,
) -> tuple[FakePool, CIVerificationObservation]:
    failed = observation or _failed_observation()
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset(
        "cs-repair",
        "demo",
        status="pr_open",
        branch="apdl/existing",
        pr_number=7,
        head_sha=failed.head_sha,
        github_pr_status="open",
        external_ci_status="pending",
    )
    now = datetime.now(timezone.utc)
    pr_result = await apply_pull_request_observation(
        pool,
        PullRequestObservation(
            observation_id=f"pr-{failed.head_sha}",
            changeset_id="cs-repair",
            repository="acme/widgets",
            pr_number=7,
            head_sha=failed.head_sha,
            status=GitHubPRStatus.open,
            action="polled",
            github_url="https://github.com/acme/widgets/pull/7",
            github_updated_at=now,
            observed_at=now,
        ),
    )
    ci_result = await apply_ci_verification_observation(pool, failed)
    assert pr_result.projected is True
    assert ci_result.projected is True
    return pool, failed


class _CountingEditor:
    def __init__(self, result: EditResult) -> None:
        self.result = result
        self.requests: list[EditRequest] = []

    async def implement(self, request: EditRequest) -> EditResult:
        self.requests.append(request)
        return self.result


@pytest.mark.asyncio
async def test_actionable_failure_repairs_same_branch_with_exact_head_lease(
    monkeypatch,
):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "2")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")
    pool, failed = await _seed_failed()
    editor = FakeEditor(
        EditResult(
            success=True,
            diff_stat={"files": 1},
            changed_paths=["src/fix.py"],
            diff_text="+fixed",
            head_sha="head-repaired",
            prompts=[
                {
                    "stage": "edit",
                    "label": "repair",
                    "user": "Fix the failing assertion.",
                }
            ],
        )
    )

    await repair_failed_ci(pool, failed, editor=editor, mint_token=_mint)

    final = await changeset_store.get_changeset(pool, "cs-repair")
    assert editor.last_request is not None
    assert editor.last_request.existing_branch is True
    assert editor.last_request.branch == "apdl/existing"
    assert editor.last_request.expected_head_sha == "head-failed"
    assert failed.failure_summary in editor.last_request.spec
    assert final.status is ChangesetStatus.pr_open
    assert final.head_sha == "head-repaired"
    assert final.external_ci_status is ExternalCIStatus.pending
    assert final.ci_retry_count == 1
    assert final.ci_remediation_status is CIRemediationStatus.awaiting_ci

    events = await list_ci_remediation_attempts(
        pool, "cs-repair", failed_head_sha="head-failed"
    )
    by_sequence = {event.event_sequence: event for event in events}
    assert set(by_sequence) == {1, 2}
    assert by_sequence[1].disposition is RemediationDisposition.diagnosing
    assert by_sequence[2].disposition is RemediationDisposition.awaiting_ci
    assert by_sequence[2].resulting_commit_sha == "head-repaired"
    assert by_sequence[2].changed_files == ["src/fix.py"]
    assert by_sequence[2].prompt_evidence_ids == [
        by_sequence[2].prompt_evidence[0].evidence_id
    ]
    assert by_sequence[1].attempt_id == by_sequence[2].attempt_id


@pytest.mark.asyncio
async def test_repeated_delivery_of_same_failure_cannot_launch_duplicate_repair(
    monkeypatch,
):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "3")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")
    pool, failed = await _seed_failed()
    editor = _CountingEditor(
        EditResult(success=False, error="agent could not repair")
    )

    await repair_failed_ci(pool, failed, editor=editor, mint_token=_mint)
    await repair_failed_ci(pool, failed, editor=editor, mint_token=_mint)

    final = await changeset_store.get_changeset(pool, "cs-repair")
    events = await list_ci_remediation_attempts(pool, "cs-repair")
    assert len(editor.requests) == 1
    assert final.status is ChangesetStatus.pr_open
    assert final.head_sha == "head-failed"
    assert final.ci_retry_count == 1
    assert final.ci_remediation_status is CIRemediationStatus.exhausted
    assert len(events) == 2
    assert {event.disposition for event in events} == {
        RemediationDisposition.diagnosing,
        RemediationDisposition.exhausted,
    }


@pytest.mark.asyncio
async def test_retry_budget_blocks_a_new_failure_scope_after_limit(monkeypatch):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "1")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")
    pool, first = await _seed_failed()
    editor = _CountingEditor(EditResult(success=False, error="still red"))

    await repair_failed_ci(pool, first, editor=editor, mint_token=_mint)
    second = _failed_observation(
        observation_id="ci-failed-2",
        check_run_id=202,
        check_suite_id=22,
        summary="tests: another assertion failed",
    )
    applied = await apply_ci_verification_observation(pool, second)
    assert applied.projected is True
    await repair_failed_ci(pool, second, editor=editor, mint_token=_mint)

    final = await changeset_store.get_changeset(pool, "cs-repair")
    events = await list_ci_remediation_attempts(pool, "cs-repair")
    assert len(editor.requests) == 1
    assert final.ci_retry_count == 1
    assert final.ci_remediation_status is CIRemediationStatus.exhausted
    assert {event.failure_observation_id for event in events} == {first.observation_id}


@pytest.mark.asyncio
async def test_policy_failure_is_recorded_but_not_edited(monkeypatch):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "2")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")
    policy_failure = _failed_observation(
        name="branch protection policy",
        summary="required review policy failed",
    )
    pool, failed = await _seed_failed(observation=policy_failure)

    class _ForbiddenEditor:
        async def implement(self, _request):
            raise AssertionError("policy failures must not invoke codegen")

    await repair_failed_ci(
        pool,
        failed,
        editor=_ForbiddenEditor(),
        mint_token=_mint,
    )

    final = await changeset_store.get_changeset(pool, "cs-repair")
    events = await list_ci_remediation_attempts(pool, "cs-repair")
    assert final.status is ChangesetStatus.pr_open
    assert final.head_sha == "head-failed"
    assert final.ci_remediation_status is CIRemediationStatus.idle
    assert {event.disposition for event in events} == {
        RemediationDisposition.diagnosing,
        RemediationDisposition.not_actionable,
    }
    terminal = next(
        event
        for event in events
        if event.disposition is RemediationDisposition.not_actionable
    )
    assert terminal.finished_at is not None
    assert "GitHub-native rerun" in (terminal.error or "")


@pytest.mark.asyncio
async def test_editor_exception_finishes_claim_as_exhausted_event(monkeypatch):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "2")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")
    pool, failed = await _seed_failed()

    class _ExplodingEditor:
        async def implement(self, _request):
            raise RuntimeError("sandbox disappeared")

    await repair_failed_ci(
        pool,
        failed,
        editor=_ExplodingEditor(),
        mint_token=_mint,
    )

    final = await changeset_store.get_changeset(pool, "cs-repair")
    events = await list_ci_remediation_attempts(pool, "cs-repair")
    exhausted = next(
        event
        for event in events
        if event.disposition is RemediationDisposition.exhausted
    )
    assert final.status is ChangesetStatus.pr_open
    assert final.ci_remediation_status is CIRemediationStatus.exhausted
    assert "sandbox disappeared" in (final.error or "")
    assert exhausted.finished_at is not None
    assert "sandbox disappeared" in (exhausted.error or "")


@pytest.mark.asyncio
async def test_stale_failed_head_cannot_start_editor(monkeypatch):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "2")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")
    pool, failed = await _seed_failed()
    pool.store["changesets"]["cs-repair"]["head_sha"] = "head-newer"

    class _ForbiddenEditor:
        async def implement(self, _request):
            raise AssertionError("stale evidence must not invoke codegen")

    await repair_failed_ci(
        pool,
        failed,
        editor=_ForbiddenEditor(),
        mint_token=_mint,
    )

    final = await changeset_store.get_changeset(pool, "cs-repair")
    events = await list_ci_remediation_attempts(pool, "cs-repair")
    assert final.head_sha == "head-newer"
    assert final.ci_retry_count == 0
    assert events == []


@pytest.mark.asyncio
async def test_repair_completion_cannot_overwrite_concurrent_github_merge(
    monkeypatch,
):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "2")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")
    pool, failed = await _seed_failed()

    class _MergeRaceEditor:
        async def implement(self, _request):
            row = pool.store["changesets"]["cs-repair"]
            row["status"] = "merged"
            row["github_pr_status"] = "merged"
            row["merge_sha"] = "github-merge-sha"
            return EditResult(
                success=True,
                changed_paths=["src/fix.py"],
                diff_text="+fixed",
                head_sha="head-too-late",
            )

    await repair_failed_ci(
        pool,
        failed,
        editor=_MergeRaceEditor(),
        mint_token=_mint,
    )

    final = await changeset_store.get_changeset(pool, "cs-repair")
    events = await list_ci_remediation_attempts(pool, "cs-repair")
    assert final.status is ChangesetStatus.merged
    assert final.github_pr_status is GitHubPRStatus.merged
    assert final.merge_sha == "github-merge-sha"
    assert any(
        event.disposition is RemediationDisposition.superseded for event in events
    )
