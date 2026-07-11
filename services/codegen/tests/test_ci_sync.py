"""GitHub PR/CI recovery keeps lifecycle separate and exact-head scoped."""

from datetime import datetime, timezone

import pytest

from app.github.checks import GitHubCIEvidence
from app.github.observations import StaleCIHeadError
from app.jobs.ci import sync_github_state
from app.models.changeset import ChangesetStatus
from app.models.observations import ExternalCIStatus, GitHubPRStatus
from app.store import changesets as store
from app.store.observations import (
    list_ci_verification_observations,
    list_pull_request_observations,
)
from tests.fakes import FakePool


async def _mint(_installation_id: int, _repo: str) -> str:
    return "ghs_tok"


def _live_pr(
    *,
    head_sha: str = "head-a",
    state: str = "open",
    draft: bool = True,
    merged: bool = False,
) -> dict:
    return {
        "number": 7,
        "head": {"sha": head_sha},
        "state": state,
        "draft": draft,
        "merged": merged,
        "merge_commit_sha": "merge-sha" if merged else None,
        "html_url": "https://github.com/acme/widgets/pull/7",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _evidence(
    head_sha: str,
    *,
    state: str | None = None,
    check_conclusion: str | None = None,
) -> GitHubCIEvidence:
    statuses = []
    if state is not None:
        statuses.append(
            {
                "sha": head_sha,
                "context": "ci / test",
                "state": state,
                "description": state,
            }
        )
    runs = []
    if check_conclusion is not None:
        runs.append(
            {
                "id": 11,
                "head_sha": head_sha,
                "name": "tests",
                "status": "completed",
                "conclusion": check_conclusion,
                "check_suite": {"id": 4, "head_sha": head_sha},
            }
        )
    return GitHubCIEvidence(
        combined_status={"sha": head_sha, "statuses": statuses},
        check_runs=runs,
    )


def _pool() -> FakePool:
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset(
        "cs-open",
        "demo",
        status="pr_open",
        pr_number=7,
        branch="apdl/x",
        head_sha="head-a",
        github_pr_status="draft",
        external_ci_status="pending",
    )
    return pool


@pytest.mark.asyncio
async def test_exact_head_pass_updates_only_external_ci_and_never_promotes_pr():
    pool = _pool()

    async def get_pr(_repo, _number, _token):
        return _live_pr()

    async def get_ci(_repo, head_sha, _token):
        return _evidence(head_sha, state="success")

    observation = await sync_github_state(
        pool,
        "cs-open",
        get_pull_request=get_pr,
        get_ci_evidence=get_ci,
        mint_token=_mint,
    )

    final = await store.get_changeset(pool, "cs-open")
    assert observation is not None
    assert observation.status is ExternalCIStatus.passed
    assert final.status is ChangesetStatus.pr_open
    assert final.external_ci_status is ExternalCIStatus.passed
    assert final.github_pr_status is GitHubPRStatus.draft


@pytest.mark.asyncio
async def test_no_signal_settles_unverified_without_leaving_pr_open_lifecycle():
    pool = _pool()

    async def get_pr(_repo, _number, _token):
        return _live_pr()

    async def get_ci(_repo, head_sha, _token):
        return _evidence(head_sha)

    await sync_github_state(
        pool,
        "cs-open",
        get_pull_request=get_pr,
        get_ci_evidence=get_ci,
        mint_token=_mint,
    )

    final = await store.get_changeset(pool, "cs-open")
    assert final.status is ChangesetStatus.pr_open
    assert final.external_ci_status is ExternalCIStatus.unverified_external_ci


@pytest.mark.asyncio
async def test_failed_exact_head_projects_failure_and_schedules_one_observation():
    pool = _pool()
    repaired = []

    async def get_pr(_repo, _number, _token):
        return _live_pr()

    async def get_ci(_repo, head_sha, _token):
        return _evidence(head_sha, check_conclusion="failure")

    async def repair(observation):
        repaired.append(observation)

    await sync_github_state(
        pool,
        "cs-open",
        get_pull_request=get_pr,
        get_ci_evidence=get_ci,
        mint_token=_mint,
        repair_failure=repair,
    )

    final = await store.get_changeset(pool, "cs-open")
    assert final.status is ChangesetStatus.pr_open
    assert final.external_ci_status is ExternalCIStatus.failed
    assert repaired and repaired[0].head_sha == "head-a"
    assert len(
        await list_ci_verification_observations(
            pool, "cs-open", head_sha="head-a"
        )
    ) == 1


@pytest.mark.asyncio
async def test_synchronize_projects_new_head_before_reading_its_ci():
    pool = _pool()
    requested = []

    async def get_pr(_repo, _number, _token):
        return _live_pr(head_sha="head-b", draft=False)

    async def get_ci(_repo, head_sha, _token):
        requested.append(head_sha)
        return _evidence(head_sha, state="success")

    await sync_github_state(
        pool,
        "cs-open",
        get_pull_request=get_pr,
        get_ci_evidence=get_ci,
        mint_token=_mint,
        pr_action="synchronize",
        delivery_id="delivery-sync",
    )

    final = await store.get_changeset(pool, "cs-open")
    assert requested == ["head-b"]
    assert final.head_sha == "head-b"
    assert final.github_pr_status is GitHubPRStatus.open
    assert final.external_ci_status is ExternalCIStatus.passed


@pytest.mark.asyncio
async def test_delayed_webhook_action_records_current_live_state_as_polled():
    pool = _pool()

    async def get_pr(_repo, _number, _token):
        # GitHub has already converted the PR back to draft after the delayed
        # ready_for_review delivery was emitted.
        return _live_pr(draft=True)

    async def get_ci(_repo, head_sha, _token):
        return _evidence(head_sha)

    await sync_github_state(
        pool,
        "cs-open",
        get_pull_request=get_pr,
        get_ci_evidence=get_ci,
        mint_token=_mint,
        pr_action="ready_for_review",
        delivery_id="delivery-delayed",
    )

    observations = await list_pull_request_observations(pool, "cs-open")
    assert observations[0].action == "polled"
    assert observations[0].delivery_id == "delivery-delayed"
    final = await store.get_changeset(pool, "cs-open")
    assert final.github_pr_status is GitHubPRStatus.draft


@pytest.mark.asyncio
async def test_stale_github_ci_payload_cannot_mark_current_head_passed():
    pool = _pool()

    async def get_pr(_repo, _number, _token):
        return _live_pr()

    async def get_ci(_repo, _head_sha, _token):
        return _evidence("head-old", state="success")

    with pytest.raises(StaleCIHeadError):
        await sync_github_state(
            pool,
            "cs-open",
            get_pull_request=get_pr,
            get_ci_evidence=get_ci,
            mint_token=_mint,
        )
    final = await store.get_changeset(pool, "cs-open")
    assert final.status is ChangesetStatus.pr_open
    assert final.external_ci_status is ExternalCIStatus.pending


@pytest.mark.asyncio
async def test_live_merged_pr_updates_lifecycle_without_reading_ci():
    pool = _pool()

    async def get_pr(_repo, _number, _token):
        return _live_pr(state="closed", merged=True, draft=False)

    async def forbidden_ci(*_args):
        raise AssertionError("merged PR must not read CI")

    result = await sync_github_state(
        pool,
        "cs-open",
        get_pull_request=get_pr,
        get_ci_evidence=forbidden_ci,
        mint_token=_mint,
        pr_action="closed",
        delivery_id="delivery-close",
    )

    final = await store.get_changeset(pool, "cs-open")
    assert result is None
    assert final.status is ChangesetStatus.merged
    assert final.github_pr_status is GitHubPRStatus.merged
    assert final.merge_sha == "merge-sha"
