"""GitHub PR/CI recovery keeps lifecycle separate and exact-head scoped."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from app.github.checks import GitHubCIEvidence
from app.github.observations import StaleCIHeadError
from app.jobs.ci import sync_github_state
from app.models.changeset import ChangesetStatus
from app.models.observations import ExternalCIStatus, GitHubPRStatus
from app.runtime.collector import RuntimeEvidenceCollection
from app.runtime.models import (
    RuntimeAcceptancePlan,
    RuntimeArtifactExpectation,
    RuntimeCheck,
    RuntimeCommand,
    RuntimeEvidenceKind,
    RuntimeSurface,
)
from app.store import changesets as store
from app.store.observations import (
    list_ci_verification_observations,
    list_pull_request_observations,
)
from app.store.runtime_evidence import list_runtime_evidence_observations
from tests.fakes import FakePool


@asynccontextmanager
async def _mint(_changeset_id: str):
    yield "ghs_tok"


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


def _runtime_plan() -> RuntimeAcceptancePlan:
    return RuntimeAcceptancePlan(
        source_ledger_sha256="a" * 64,
        repo_profile_sha256="b" * 64,
        verification_plan_sha256="c" * 64,
        repo="acme/widgets",
        branch="apdl/x",
        checks=[
            RuntimeCheck(
                check_id="runtime_aaaaaaaaaaaaaaaa",
                surface=RuntimeSurface.runtime,
                requirement_ids=["REQ-001"],
                command=RuntimeCommand(
                    command="npm test", cwd=".", source_path="package.json"
                ),
                expected_artifacts=[
                    RuntimeArtifactExpectation(
                        artifact_name="apdl-runtime-evidence",
                        evidence_kind=RuntimeEvidenceKind.structured_runtime,
                        paths=["apdl-runtime-evidence.json"],
                        requirement_ids=["REQ-001"],
                    )
                ],
            )
        ],
    )


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
async def test_runtime_evidence_is_journaled_for_exact_head_before_repair():
    pool = _pool()
    plan = _runtime_plan()
    pool.store["changesets"]["cs-open"]["runtime_acceptance_plan"] = (
        plan.model_dump(mode="json")
    )
    order: list[str] = []

    async def get_pr(_repo, _number, _token):
        return _live_pr()

    async def get_ci(_repo, head_sha, _token):
        return _evidence(head_sha, check_conclusion="failure")

    async def collect(repo, head_sha, token, runtime_plan):
        assert (repo, head_sha, token) == ("acme/widgets", "head-a", "ghs_tok")
        assert runtime_plan == plan
        order.append("collect")
        return RuntimeEvidenceCollection(head_sha=head_sha)

    async def repair(observation):
        evidence = await list_runtime_evidence_observations(
            pool, observation.changeset_id, head_sha=observation.head_sha
        )
        assert len(evidence) == 1
        assert evidence[0].assessment.external_ci_status is ExternalCIStatus.failed
        order.append("repair")

    await sync_github_state(
        pool,
        "cs-open",
        get_pull_request=get_pr,
        get_ci_evidence=get_ci,
        mint_token=_mint,
        repair_failure=repair,
        collect_runtime=collect,
    )

    assert order == ["collect", "repair"]
    final = await store.get_changeset(pool, "cs-open")
    assert final is not None
    assert final.external_ci_status is ExternalCIStatus.failed
    assert final.runtime_evidence_assessment is not None
    assert final.runtime_evidence_assessment.external_ci_status is ExternalCIStatus.failed


@pytest.mark.asyncio
async def test_repeated_ci_observation_collects_runtime_evidence_once():
    pool = _pool()
    plan = _runtime_plan()
    pool.store["changesets"]["cs-open"]["runtime_acceptance_plan"] = (
        plan.model_dump(mode="json")
    )
    collections = 0

    async def get_pr(_repo, _number, _token):
        return _live_pr()

    async def get_ci(_repo, head_sha, _token):
        return _evidence(head_sha, state="success")

    async def collect(_repo, head_sha, _token, _plan):
        nonlocal collections
        collections += 1
        return RuntimeEvidenceCollection(head_sha=head_sha)

    for _ in range(2):
        await sync_github_state(
            pool,
            "cs-open",
            get_pull_request=get_pr,
            get_ci_evidence=get_ci,
            mint_token=_mint,
            collect_runtime=collect,
        )

    assert collections == 1
    assert len(await list_runtime_evidence_observations(pool, "cs-open")) == 1


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
