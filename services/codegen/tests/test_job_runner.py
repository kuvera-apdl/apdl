"""Tests for the changeset job runner (fake editor + fake pool, no network)."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.contracts.models import ContractBundle
from app.editor.base import EditRequest, EditResult
from app.editor.fake import FakeEditor
from app.evaluations.models import RolloutStage
from app.github.app_auth import AuthorizedRepositoryTarget, InstallationToken
from app.github.pulls import PullRequest
from app.github.token_broker import GitHubTokenBroker
from app.inspection.models import DependencySlice, InspectionSnapshot
from app.jobs.runner import run_changeset_job as _run_changeset_job
from app.models.changeset import ChangesetStatus
from app.models.connection import RepositoryTarget
from app.models.observations import ExternalCIStatus, GitHubPRStatus
from app.publication import ConfiguredPublicationGate
from app.profiling import RepoProfile
from app.profiling.models import (
    CIWorkflow,
    CommandKind,
    RepoCommand,
    TestFacility as ProfileTestFacility,
)
from app.requirements import compile_requirement_ledger, map_implementation_evidence
from app.runtime.models import (
    GeneratedRuntimeWorkflowAttestation,
    GeneratedRuntimeWorkflowExpectation,
    RuntimeAcceptancePlan,
    RuntimeArtifactExpectation,
    RuntimeCheck,
    RuntimeCommand,
    RuntimeEvidenceKind,
    RuntimeSurface,
    RuntimeAcceptanceRequest,
)
from app.safety.policy import (
    PlatformCodegenSafetyPolicy,
    TenantCodegenConnectionPolicy,
    TenantCodegenGatesPolicy,
    resolve_effective_policy,
)
from app.semantic_review import assemble_review_verdict
from app.store import changesets as store
from app.verification import build_verification_plan, evaluate_verification_coverage
from tests.fakes import FakePool
from tests.publication_fakes import (
    allowing_publication_gate,
    denying_publication_gate,
)

_TASK = {
    "title": "Add dark mode",
    "spec": "Implement a dark-mode toggle.",
    "context": {},
    "constraints": ["keeps existing tests green"],
}


async def run_changeset_job(*args, publication_gate=None, **kwargs):
    """Exercise the production runner with an explicit trusted test gate."""
    return await _run_changeset_job(
        *args,
        publication_gate=publication_gate or allowing_publication_gate(),
        **kwargs,
    )


@asynccontextmanager
async def _mint(_changeset_id: str):
    yield "ghs_tok"


def _repository_target(pool: FakePool, project_id: str = "demo") -> RepositoryTarget:
    connection = pool.store["connections"][project_id]
    grant = pool.store["repository_grants"][connection["grant_id"]]
    return RepositoryTarget(
        grant_id=grant["grant_id"],
        project_id=project_id,
        installation_id=grant["installation_id"],
        repository_id=grant["repository_id"],
        repository_full_name=grant["repository_full_name"],
    )


async def _seed(pool: FakePool, changeset_id: str, project_id: str = "demo", base="main"):
    await store.create_changeset(
        pool,
        changeset_id=changeset_id,
        project_id=project_id,
        run_id="run-1",
        base_branch=base,
        task=_TASK,
        repository_target=_repository_target(pool, project_id),
        tenant_policy_snapshot=TenantCodegenConnectionPolicy(),
    )


def _pr(
    number: int,
    *,
    head_sha: str = "fake-head-sha",
    status: GitHubPRStatus = GitHubPRStatus.open,
) -> PullRequest:
    return PullRequest(
        url=f"https://github.com/acme/widgets/pull/{number}",
        number=number,
        head_sha=head_sha,
        status=status,
        github_updated_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )


def _implemented_ledger(paths: list[str]):
    return map_implementation_evidence(
        compile_requirement_ledger(
            title=_TASK["title"],
            spec=_TASK["spec"],
            constraints=_TASK["constraints"],
        ),
        paths,
    )


def _profile_with_github_ci() -> RepoProfile:
    return RepoProfile(
        commands=[
            RepoCommand(
                kind=CommandKind.test,
                command="npm test",
                cwd=".",
                source_path="package.json",
            )
        ],
        test_facilities=[
            ProfileTestFacility(
                name="vitest", package_path=".", source_path="package.json"
            )
        ],
        ci_workflows=[
            CIWorkflow(provider="github_actions", path=".github/workflows/ci.yml")
        ],
    )


def _workflow_bound_plan() -> RuntimeAcceptancePlan:
    return RuntimeAcceptancePlan(
        source_ledger_sha256="a" * 64,
        repo_profile_sha256="b" * 64,
        verification_plan_sha256="c" * 64,
        checks=[
            RuntimeCheck(
                check_id="runtime_0123456789abcdef",
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
        generated_workflow=GeneratedRuntimeWorkflowExpectation(
            path=".github/workflows/apdl-runtime-acceptance.yml",
            content_sha256="d" * 64,
        ),
    )


@pytest.mark.asyncio
async def test_job_opens_ready_pr_for_low_risk_change_with_external_ci():
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets", installation_id=1)
    await _seed(pool, "cs_abc12345")

    ledger = _implemented_ledger(["src/theme.ts"])
    editor = FakeEditor(
        EditResult(
            success=True,
            diff_stat={"files": 3, "additions": 10, "deletions": 0},
            changed_paths=["src/theme.ts"],
            requirement_ledger=ledger,
            verification_plan=build_verification_plan(
                ledger, _profile_with_github_ci()
            ),
        )
    )
    calls: dict = {}
    leased: list[str] = []

    @asynccontextmanager
    async def mint(changeset_id: str):
        leased.append(changeset_id)
        yield "ghs_tok"

    async def open_pr(**kwargs) -> PullRequest:
        calls.update(kwargs)
        return _pr(9)

    await run_changeset_job(
        pool,
        "cs_abc12345",
        editor=editor,
        mint_token=mint,
        open_pr=open_pr,
    )

    final = await store.get_changeset(pool, "cs_abc12345")
    assert final.status == ChangesetStatus.pr_open
    assert final.pr_url.endswith("/pull/9")
    assert final.pr_number == 9
    assert final.head_sha == "fake-head-sha"
    assert final.github_pr_status is GitHubPRStatus.open
    assert final.external_ci_status is ExternalCIStatus.pending
    assert final.branch.startswith("apdl/add-dark-mode-")
    assert final.diff_stat == {"files": 3, "additions": 10, "deletions": 0}
    assert final.requirement_ledger is not None
    assert final.requirement_ledger.ready_for_pull_request()
    assert final.publication_authorization is not None
    assert final.publication_authorization.decision.ready_for_review is True
    # The editor saw the resolved repo/branch + the minted token.
    assert isinstance(editor.last_request, EditRequest)
    assert editor.last_request.repo == "acme/widgets"
    assert editor.last_request.base_branch == "main"
    assert editor.last_request.token == "ghs_tok"
    # Low-risk work with external CI is ready for GitHub review immediately.
    assert calls["draft"] is False
    assert calls["repo"] == "acme/widgets"
    assert calls["base"] == "main"
    assert calls["token"] == "ghs_tok"
    assert leased == ["cs_abc12345", "cs_abc12345"]
    assert "## Requirement ledger" in calls["body"]
    assert "`REQ-001`" in calls["body"]
    assert "## Publication rollout" in calls["body"]
    assert "low_risk_canary" in calls["body"]
    assert final.publication_authorization.authorization_sha256 in calls["body"]


@pytest.mark.asyncio
async def test_revocation_after_editor_push_blocks_fresh_pr_lease():
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_revoked1")
    grant_id = pool.store["connections"]["demo"]["grant_id"]

    ledger = _implemented_ledger(["src/theme.ts"])
    editor = FakeEditor(
        EditResult(
            success=True,
            diff_stat={"files": 1, "additions": 4, "deletions": 0},
            changed_paths=["src/theme.ts"],
            requirement_ledger=ledger,
            verification_plan=build_verification_plan(
                ledger, _profile_with_github_ci()
            ),
        )
    )
    issued: list[AuthorizedRepositoryTarget] = []
    revoked: list[str] = []

    async def issue(target, *, permissions):
        issued.append(target)
        return InstallationToken(
            token="ghs_editor",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    async def revoke(token: str) -> None:
        revoked.append(token)
        pool.store["repository_grants"][grant_id]["status"] = "revoked"

    broker = GitHubTokenBroker(pool, issue_token=issue, revoke_token=revoke)

    async def open_pr(**kwargs) -> PullRequest:
        raise AssertionError(f"revoked authority opened a PR: {kwargs}")

    await run_changeset_job(
        pool,
        "cs_revoked1",
        editor=editor,
        mint_token=broker.write_changeset,
        open_pr=open_pr,
    )

    final = await store.get_changeset(pool, "cs_revoked1")
    assert final.status == ChangesetStatus.error
    assert "no active repository grant" in final.error
    assert len(issued) == 1
    assert revoked == ["ghs_editor"]
    assert final.pr_number is None


@pytest.mark.asyncio
async def test_token_cleanup_failure_does_not_orphan_an_opened_pr():
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_cleanup1")

    ledger = _implemented_ledger(["src/theme.ts"])
    editor = FakeEditor(
        EditResult(
            success=True,
            diff_stat={"files": 1, "additions": 4, "deletions": 0},
            changed_paths=["src/theme.ts"],
            requirement_ledger=ledger,
            verification_plan=build_verification_plan(
                ledger, _profile_with_github_ci()
            ),
        )
    )

    async def issue(target, *, permissions):
        return InstallationToken(
            token="ghs_cleanup",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    async def revoke(token: str) -> None:
        raise RuntimeError("GitHub cleanup unavailable")

    broker = GitHubTokenBroker(pool, issue_token=issue, revoke_token=revoke)

    async def open_pr(**kwargs) -> PullRequest:
        return _pr(11)

    await run_changeset_job(
        pool,
        "cs_cleanup1",
        editor=editor,
        mint_token=broker.write_changeset,
        open_pr=open_pr,
    )

    final = await store.get_changeset(pool, "cs_cleanup1")
    assert final.status == ChangesetStatus.pr_open
    assert final.pr_number == 11
    assert final.pr_url.endswith("/pull/11")


@pytest.mark.asyncio
async def test_cancellation_during_pr_token_cleanup_keeps_accepted_pr_projected():
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_cancelpr")

    ledger = _implemented_ledger(["src/theme.ts"])
    editor = FakeEditor(
        EditResult(
            success=True,
            diff_stat={"files": 1, "additions": 4, "deletions": 0},
            changed_paths=["src/theme.ts"],
            requirement_ledger=ledger,
            verification_plan=build_verification_plan(
                ledger, _profile_with_github_ci()
            ),
        )
    )
    cleanup_started = asyncio.Event()
    revoke_count = 0

    async def issue(target, *, permissions):
        return InstallationToken(
            token="ghs_cancel",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    async def revoke(token: str) -> None:
        nonlocal revoke_count
        revoke_count += 1
        if revoke_count == 2:
            cleanup_started.set()
            await asyncio.Event().wait()

    broker = GitHubTokenBroker(pool, issue_token=issue, revoke_token=revoke)

    async def open_pr(**kwargs) -> PullRequest:
        return _pr(44)

    job = asyncio.create_task(
        run_changeset_job(
            pool,
            "cs_cancelpr",
            editor=editor,
            mint_token=broker.write_changeset,
            open_pr=open_pr,
        )
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job

    final = await store.get_changeset(pool, "cs_cancelpr")
    assert final.status == ChangesetStatus.pr_open
    assert final.pr_number == 44
    assert final.pr_url.endswith("/pull/44")


@pytest.mark.asyncio
async def test_job_marks_failed_generation_as_error_without_opening_pr():
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_fail0001")

    editor = FakeEditor(EditResult(success=False, error="tests red"))
    opened: list = []

    async def open_pr(**kwargs) -> PullRequest:
        opened.append(kwargs)
        return _pr(1)

    await run_changeset_job(pool, "cs_fail0001", editor=editor, mint_token=_mint, open_pr=open_pr)

    final = await store.get_changeset(pool, "cs_fail0001")
    assert final.status == ChangesetStatus.error
    assert final.error == "tests red"
    assert opened == []


@pytest.mark.asyncio
async def test_rollout_denial_never_mints_token_or_invokes_editor():
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_rollout_denied")
    editor = FakeEditor()
    minted: list[str] = []

    @asynccontextmanager
    async def mint(changeset_id: str):
        minted.append(changeset_id)
        yield "must-not-be-returned"

    async def open_pr(**_kwargs) -> PullRequest:
        raise AssertionError("PR should not be opened")

    gate = denying_publication_gate()
    await run_changeset_job(
        pool,
        "cs_rollout_denied",
        editor=editor,
        mint_token=mint,
        open_pr=open_pr,
        publication_gate=gate,
    )

    final = await store.get_changeset(pool, "cs_rollout_denied")
    assert final is not None
    assert final.status is ChangesetStatus.error
    assert "before GitHub credential minting" in (final.error or "")
    assert final.publication_authorization is not None
    assert final.publication_authorization.decision.allowed is False
    assert minted == []
    assert editor.last_request is None


@pytest.mark.asyncio
async def test_offline_stage_has_no_github_write_capability():
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_offline")
    editor = FakeEditor()
    minted: list[str] = []

    @asynccontextmanager
    async def mint(changeset_id: str):
        minted.append(changeset_id)
        yield "must-not-be-returned"

    async def open_pr(**_kwargs) -> PullRequest:
        raise AssertionError("PR should not be opened")

    await run_changeset_job(
        pool,
        "cs_offline",
        editor=editor,
        mint_token=mint,
        open_pr=open_pr,
        publication_gate=ConfiguredPublicationGate(
            stage=RolloutStage.offline,
            model="test-model@1",
            codegen_revision="test-revision",
        ),
    )

    final = await store.get_changeset(pool, "cs_offline")
    assert final is not None
    assert final.status is ChangesetStatus.error
    assert "offline rollout stage cannot publish" in (final.error or "")
    assert final.publication_authorization is None
    assert minted == []
    assert editor.last_request is None


@pytest.mark.asyncio
async def test_job_errors_when_repository_grant_is_revoked():
    pool = FakePool()
    pool.add_connection("ghost")
    await store.create_changeset(
        pool, changeset_id="cs_ghost001", project_id="ghost",
        run_id=None, base_branch="main", task=_TASK,
        repository_target=_repository_target(pool, "ghost"),
        tenant_policy_snapshot=TenantCodegenConnectionPolicy(),
    )
    grant_id = pool.store["connections"]["ghost"]["grant_id"]
    pool.store["repository_grants"][grant_id]["status"] = "revoked"

    async def open_pr(**kwargs) -> PullRequest:
        raise AssertionError("PR should not be opened")

    await run_changeset_job(
        pool, "cs_ghost001", editor=FakeEditor(), mint_token=_mint, open_pr=open_pr
    )

    final = await store.get_changeset(pool, "cs_ghost001")
    assert final.status == ChangesetStatus.error


@pytest.mark.asyncio
async def test_job_errors_on_unexpected_editor_fault():
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_boom0001")

    class _BoomEditor:
        async def implement(self, request: EditRequest) -> EditResult:
            raise RuntimeError("kaboom")

    async def open_pr(**kwargs) -> PullRequest:
        raise AssertionError("PR should not be opened")

    await run_changeset_job(
        pool, "cs_boom0001", editor=_BoomEditor(), mint_token=_mint, open_pr=open_pr
    )

    final = await store.get_changeset(pool, "cs_boom0001")
    assert final.status == ChangesetStatus.error
    assert "kaboom" in (final.error or "")


@pytest.mark.asyncio
async def test_job_is_a_noop_for_unknown_changeset():
    async def open_pr(**kwargs) -> PullRequest:
        raise AssertionError("PR should not be opened")

    # Should not raise.
    await run_changeset_job(
        FakePool(), "cs_missing", editor=FakeEditor(), mint_token=_mint, open_pr=open_pr
    )


@pytest.mark.asyncio
async def test_job_blocks_on_pre_push_gate_violation():
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_secret01")

    editor = FakeEditor(
        EditResult(
            success=True,
            diff_stat={"files": 1, "additions": 1, "deletions": 0},
            diff_text="AKIAIOSFODNN7EXAMPLE",
        )
    )
    opened: list = []

    async def open_pr(**kwargs) -> PullRequest:
        opened.append(kwargs)
        return _pr(1)

    await run_changeset_job(pool, "cs_secret01", editor=editor, mint_token=_mint, open_pr=open_pr)

    final = await store.get_changeset(pool, "cs_secret01")
    assert final.status == ChangesetStatus.error
    assert "gate" in (final.error or "").lower()
    assert opened == []


@pytest.mark.asyncio
async def test_policy_alone_cannot_bypass_workflow_gate_without_attestation():
    pool = FakePool()
    pool.add_connection(
        "demo",
        tenant_policy=TenantCodegenConnectionPolicy(
            runtime_acceptance=RuntimeAcceptanceRequest(enabled=True)
        ),
    )
    await _seed(pool, "cs_workflow_guard")
    workflow = ".github/workflows/apdl-runtime-acceptance.yml"
    editor = FakeEditor(
        EditResult(
            success=True,
            diff_stat={"files": 1, "additions": 5, "deletions": 0},
            changed_paths=[workflow],
            diff_text=f"diff --git a/{workflow} b/{workflow}\n+permissions: write-all",
        )
    )
    opened: list = []

    async def open_pr(**kwargs) -> PullRequest:
        opened.append(kwargs)
        return _pr(1)

    await run_changeset_job(
        pool,
        "cs_workflow_guard",
        editor=editor,
        mint_token=_mint,
        open_pr=open_pr,
        publication_gate=allowing_publication_gate(RolloutStage.reviewed_pr),
    )

    final = await store.get_changeset(pool, "cs_workflow_guard")
    assert final is not None
    assert final.status is ChangesetStatus.error
    assert "protected path" in (final.error or "")
    assert opened == []


@pytest.mark.asyncio
async def test_forged_workflow_content_attestation_cannot_bypass_runner_gate():
    pool = FakePool()
    pool.add_connection(
        "demo",
        tenant_policy=TenantCodegenConnectionPolicy(
            runtime_acceptance=RuntimeAcceptanceRequest(enabled=True)
        ),
    )
    await _seed(pool, "cs_workflow_forgery")
    workflow = ".github/workflows/apdl-runtime-acceptance.yml"
    plan = _workflow_bound_plan()
    editor = FakeEditor(
        EditResult(
            success=True,
            diff_stat={"files": 1, "additions": 1, "deletions": 0},
            changed_paths=[workflow],
            diff_text=f"diff --git a/{workflow} b/{workflow}\n+permissions: write-all",
            requirement_ledger=_implemented_ledger([workflow]),
            runtime_acceptance_plan=plan,
            generated_runtime_workflow=GeneratedRuntimeWorkflowAttestation(
                path=workflow,
                content_sha256="e" * 64,
                runtime_acceptance_plan_sha256=plan.evidence_hash(),
            ),
        )
    )
    opened: list = []

    async def open_pr(**kwargs) -> PullRequest:
        opened.append(kwargs)
        return _pr(1)

    await run_changeset_job(
        pool,
        "cs_workflow_forgery",
        editor=editor,
        mint_token=_mint,
        open_pr=open_pr,
        platform_safety_policy=PlatformCodegenSafetyPolicy(
            runtime_workflow_generation_enabled=True
        ),
    )

    final = await store.get_changeset(pool, "cs_workflow_forgery")
    assert final is not None
    assert final.status is ChangesetStatus.error
    assert "protected path" in (final.error or "")
    assert opened == []


@pytest.mark.asyncio
async def test_job_backs_off_when_already_claimed():
    # The queued → cloning transition is the claim; a duplicate job (double
    # enqueue, concurrent replica) must not touch the winner's changeset.
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_claimed", "demo", status="cloning")

    editor = FakeEditor()

    async def open_pr(**kwargs) -> PullRequest:
        raise AssertionError("PR should not be opened")

    await run_changeset_job(
        pool, "cs_claimed", editor=editor, mint_token=_mint, open_pr=open_pr
    )

    final = await store.get_changeset(pool, "cs_claimed")
    assert final.status == ChangesetStatus.cloning  # winner's state untouched
    assert final.error is None
    assert editor.last_request is None  # the loser never ran the editor


@pytest.mark.asyncio
async def test_job_passes_connection_policy_and_revert_sha_to_the_editor():
    pool = FakePool()
    pool.add_connection(
        "demo",
        tenant_policy=TenantCodegenConnectionPolicy(
            test_cmd="make ci",
            gates=TenantCodegenGatesPolicy(max_files=5),
        ),
    )
    task = {**_TASK, "context": {"revert_sha": "cafebabe"}}
    await store.create_changeset(
        pool, changeset_id="cs_pol00001", project_id="demo",
        run_id=None, base_branch="main", task=task,
        repository_target=_repository_target(pool),
        tenant_policy_snapshot=TenantCodegenConnectionPolicy(
            test_cmd="make ci",
            gates=TenantCodegenGatesPolicy(max_files=5),
        ),
    )

    editor = FakeEditor(
        EditResult(
            success=True,
            diff_stat={"files": 1, "additions": 1, "deletions": 0},
        )
    )

    async def open_pr(**kwargs) -> PullRequest:
        return _pr(2)

    await run_changeset_job(
        pool, "cs_pol00001", editor=editor, mint_token=_mint, open_pr=open_pr
    )

    assert editor.last_request.test_cmd == "make ci"
    assert editor.last_request.safety_policy.max_files == 5
    assert editor.last_request.revert_sha == "cafebabe"


@pytest.mark.asyncio
async def test_job_uses_queued_tenant_policy_snapshot_after_connection_is_weakened():
    pool = FakePool()
    strict_snapshot = TenantCodegenConnectionPolicy(
        gates=TenantCodegenGatesPolicy(max_files=5)
    )
    pool.add_connection(
        "demo",
        tenant_policy=TenantCodegenConnectionPolicy(
            gates=TenantCodegenGatesPolicy(max_files=50)
        ),
    )
    await store.create_changeset(
        pool,
        changeset_id="cs_snapshot1",
        project_id="demo",
        run_id=None,
        base_branch="main",
        task=_TASK,
        repository_target=_repository_target(pool),
        tenant_policy_snapshot=strict_snapshot,
    )
    editor = FakeEditor(
        EditResult(
            success=True,
            diff_stat={"files": 6, "additions": 6, "deletions": 0},
            changed_paths=[f"src/file-{index}.py" for index in range(6)],
        )
    )
    opened: list[dict] = []

    async def open_pr(**kwargs) -> PullRequest:
        opened.append(kwargs)
        return _pr(2)

    await run_changeset_job(
        pool,
        "cs_snapshot1",
        editor=editor,
        mint_token=_mint,
        open_pr=open_pr,
    )

    final = await store.get_changeset(pool, "cs_snapshot1")
    assert final.status is ChangesetStatus.error
    assert "5-file limit" in (final.error or "")
    assert editor.last_request is not None
    assert editor.last_request.safety_policy.max_files == 5
    assert final.tenant_policy_snapshot == strict_snapshot
    assert final.effective_safety_policy_sha256 == resolve_effective_policy(
        strict_snapshot, PlatformCodegenSafetyPolicy()
    ).canonical_digest()
    assert opened == []


@pytest.mark.asyncio
async def test_job_loads_configured_platform_policy_when_not_injected(
    monkeypatch,
    tmp_path,
):
    policy_path = tmp_path / "platform-safety.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": "platform_codegen_safety_policy@1",
                "max_files": 1,
                "max_lines": 2000,
                "additional_protected_paths": [],
                "runtime_workflow_generation_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEGEN_PLATFORM_SAFETY_POLICY_PATH", str(policy_path))
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_platform")
    editor = FakeEditor(
        EditResult(
            success=True,
            diff_stat={"files": 2, "additions": 2, "deletions": 0},
            changed_paths=["src/one.py", "src/two.py"],
        )
    )

    async def open_pr(**_kwargs) -> PullRequest:
        raise AssertionError("PR should not be opened")

    await run_changeset_job(
        pool,
        "cs_platform",
        editor=editor,
        mint_token=_mint,
        open_pr=open_pr,
    )

    final = await store.get_changeset(pool, "cs_platform")
    assert final.status is ChangesetStatus.error
    assert "1-file limit" in (final.error or "")
    assert editor.last_request is not None
    assert editor.last_request.safety_policy.max_files == 1


@pytest.mark.asyncio
async def test_malformed_stored_tenant_policy_fails_before_token_minting():
    pool = FakePool()
    pool.add_connection(
        "demo",
        tenant_policy={
            "schema_version": "tenant_codegen_connection_policy@1",
            "gates": {"protected_paths": []},
        },
    )
    await _seed(pool, "cs_badpolicy")
    pool.store["changesets"]["cs_badpolicy"]["tenant_policy_snapshot"] = json.dumps(
        {
            "schema_version": "tenant_codegen_connection_policy@1",
            "gates": {"protected_paths": []},
        }
    )
    minted: list[str] = []
    editor = FakeEditor()

    @asynccontextmanager
    async def mint(changeset_id: str):
        minted.append(changeset_id)
        yield "must-not-be-returned"

    async def open_pr(**_kwargs) -> PullRequest:
        raise AssertionError("PR should not be opened")

    await run_changeset_job(
        pool,
        "cs_badpolicy",
        editor=editor,
        mint_token=mint,
        open_pr=open_pr,
    )

    final = pool.store["changesets"]["cs_badpolicy"]
    assert final["status"] == ChangesetStatus.error.value
    assert "protected_paths" in (final["error"] or "")
    assert minted == []
    assert editor.last_request is None


@pytest.mark.asyncio
async def test_job_abandoned_when_automation_disabled(monkeypatch):
    monkeypatch.setenv("CODEGEN_KILL_SWITCH", "true")
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_killed01")

    async def open_pr(**kwargs) -> PullRequest:
        raise AssertionError("PR should not be opened")

    await run_changeset_job(
        pool, "cs_killed01", editor=FakeEditor(), mint_token=_mint, open_pr=open_pr
    )

    final = await store.get_changeset(pool, "cs_killed01")
    assert final.status == ChangesetStatus.abandoned


class _BlockingEditor:
    """Blocks in implement() until released, to observe the concurrency slot."""

    def __init__(self) -> None:
        self.started = 0
        self.first_started = asyncio.Event()
        self.release = asyncio.Event()

    async def implement(self, request: EditRequest) -> EditResult:
        self.started += 1
        self.first_started.set()
        await self.release.wait()
        return EditResult(
            success=True,
            branch=request.branch,
            diff_stat={"files": 1, "additions": 1, "deletions": 0},
            head_sha="fake-head-sha",
        )


@pytest.mark.asyncio
async def test_jobs_serialize_at_concurrency_one(monkeypatch):
    # Force a fresh slot bound to this loop; default concurrency is 1.
    import app.jobs.runner as runner_mod

    monkeypatch.setattr(runner_mod, "_job_semaphore", None)

    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets", installation_id=1)
    await _seed(pool, "cs_one")
    await _seed(pool, "cs_two")

    editor = _BlockingEditor()

    async def open_pr(**kwargs) -> PullRequest:
        return _pr(1)

    t1 = asyncio.create_task(
        run_changeset_job(pool, "cs_one", editor=editor, mint_token=_mint, open_pr=open_pr)
    )
    t2 = asyncio.create_task(
        run_changeset_job(pool, "cs_two", editor=editor, mint_token=_mint, open_pr=open_pr)
    )

    # First job reaches the editor; let the loop spin so the second would too if unbounded.
    await asyncio.wait_for(editor.first_started.wait(), timeout=1)
    await asyncio.sleep(0.05)

    # Only one job is in-flight; the other waits at the slot, still queued.
    assert editor.started == 1
    assert (await store.get_changeset(pool, "cs_one")).status == ChangesetStatus.editing
    assert (await store.get_changeset(pool, "cs_two")).status == ChangesetStatus.queued

    editor.release.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2)

    assert editor.started == 2
    assert (await store.get_changeset(pool, "cs_one")).status == ChangesetStatus.pr_open
    assert (await store.get_changeset(pool, "cs_two")).status == ChangesetStatus.pr_open


@pytest.mark.asyncio
async def test_job_persists_prompt_transcript_on_success_and_failure():
    """EditResult.prompts lands on the changeset row either way the edit ends."""
    transcript = [
        {"stage": "edit", "label": "Edit instruction (attempt 1)",
         "system": None, "user": "do the thing", "notes": None},
    ]

    async def open_pr(**kwargs) -> PullRequest:
        return _pr(9)

    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_prompt_ok")
    await _seed(pool, "cs_prompt_ko")

    editor = FakeEditor(
        EditResult(
            success=True,
            diff_stat={"files": 1, "additions": 1, "deletions": 0},
            prompts=transcript,
        )
    )
    await run_changeset_job(pool, "cs_prompt_ok", editor=editor, mint_token=_mint, open_pr=open_pr)
    ok = await store.get_changeset(pool, "cs_prompt_ok")
    assert ok.status == ChangesetStatus.pr_open
    assert ok.prompts == transcript

    editor = FakeEditor(EditResult(success=False, error="tests red", prompts=transcript))
    await run_changeset_job(pool, "cs_prompt_ko", editor=editor, mint_token=_mint, open_pr=open_pr)
    ko = await store.get_changeset(pool, "cs_prompt_ko")
    assert ko.status == ChangesetStatus.error
    assert ko.prompts == transcript


@pytest.mark.asyncio
async def test_job_persists_contract_evidence_without_changing_ci_status():
    async def open_pr(**kwargs) -> PullRequest:
        return _pr(10)

    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_contracts")
    bundle = ContractBundle()

    await run_changeset_job(
        pool,
        "cs_contracts",
        editor=FakeEditor(
            EditResult(
                success=True,
                diff_stat={"files": 1, "additions": 1, "deletions": 0},
                contract_bundle=bundle,
            )
        ),
        mint_token=_mint,
        open_pr=open_pr,
    )

    stored = await store.get_changeset(pool, "cs_contracts")
    assert stored.contract_bundle == bundle
    assert stored.status is ChangesetStatus.pr_open
    assert stored.external_ci_status is ExternalCIStatus.pending


@pytest.mark.asyncio
async def test_job_persists_repository_inspection_evidence():
    async def open_pr(**kwargs) -> PullRequest:
        return _pr(11)

    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_inspection")
    snapshot = InspectionSnapshot()
    dependency_slice = DependencySlice()

    await run_changeset_job(
        pool,
        "cs_inspection",
        editor=FakeEditor(
            EditResult(
                success=True,
                diff_stat={"files": 1, "additions": 1, "deletions": 0},
                inspection_snapshot=snapshot,
                dependency_slice=dependency_slice,
            )
        ),
        mint_token=_mint,
        open_pr=open_pr,
    )

    stored = await store.get_changeset(pool, "cs_inspection")
    assert stored.inspection_snapshot == snapshot
    assert stored.dependency_slice == dependency_slice


@pytest.mark.asyncio
async def test_job_persists_verification_evidence_and_labels_pr_body_as_expected():
    calls: dict = {}

    async def open_pr(**kwargs) -> PullRequest:
        calls.update(kwargs)
        return _pr(12, status=GitHubPRStatus.draft)

    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_verification")
    ledger = _implemented_ledger(["src/theme.ts", "tests/theme.test.ts"])
    profile = _profile_with_github_ci()
    plan = build_verification_plan(ledger, profile)
    coverage = evaluate_verification_coverage(
        plan, changed_paths=["src/theme.ts", "tests/theme.test.ts"]
    )
    review = assemble_review_verdict(
        ledger=ledger,
        contracts=ContractBundle(),
        dependency_slice=DependencySlice(),
        verification_plan=plan,
        verification_coverage=coverage,
        diff_text="diff --git a/src/theme.ts b/src/theme.ts\n+changed",
        model_response_text=None,
    )

    await run_changeset_job(
        pool,
        "cs_verification",
        editor=FakeEditor(
            EditResult(
                success=True,
                diff_stat={"files": 2, "additions": 2, "deletions": 0},
                changed_paths=["src/theme.ts", "tests/theme.test.ts"],
                requirement_ledger=ledger,
                verification_plan=plan,
                verification_coverage=coverage,
                review_verdict=review,
            )
        ),
        mint_token=_mint,
        open_pr=open_pr,
    )

    stored = await store.get_changeset(pool, "cs_verification")
    assert stored.verification_plan == plan
    assert stored.verification_coverage == coverage
    assert stored.review_verdict == review
    assert stored.external_ci_status is ExternalCIStatus.pending
    assert "## Verification coverage" in calls["body"]
    assert "GitHub CI is authoritative" in calls["body"]
    assert "## Semantic review" in calls["body"]
    assert review.reviewed_diff_sha256 in calls["body"]
    assert "passed" not in calls["body"].lower()


@pytest.mark.asyncio
async def test_job_opens_draft_pr_for_higher_risk_change():
    pool = FakePool()
    pool.add_connection("demo")
    task = {**_TASK, "context": {"risk_level": "medium"}}
    await store.create_changeset(
        pool,
        changeset_id="cs_risk0001",
        project_id="demo",
        run_id=None,
        base_branch="main",
        task=task,
        repository_target=_repository_target(pool),
        tenant_policy_snapshot=TenantCodegenConnectionPolicy(),
    )
    calls: dict = {}

    async def open_pr(**kwargs) -> PullRequest:
        calls.update(kwargs)
        return _pr(13, status=GitHubPRStatus.draft)

    await run_changeset_job(
        pool,
        "cs_risk0001",
        editor=FakeEditor(),
        mint_token=_mint,
        open_pr=open_pr,
        publication_gate=allowing_publication_gate(RolloutStage.reviewed_pr),
    )

    stored = await store.get_changeset(pool, "cs_risk0001")
    assert stored.status is ChangesetStatus.pr_open
    assert stored.github_pr_status is GitHubPRStatus.draft
    assert calls["draft"] is True


@pytest.mark.asyncio
async def test_job_opens_draft_and_records_unverified_when_repo_has_no_ci():
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_noci0001")
    ledger = _implemented_ledger(["src/theme.ts"])
    plan = build_verification_plan(ledger, RepoProfile())
    calls: dict = {}

    async def open_pr(**kwargs) -> PullRequest:
        calls.update(kwargs)
        return _pr(14, status=GitHubPRStatus.draft)

    await run_changeset_job(
        pool,
        "cs_noci0001",
        editor=FakeEditor(
            EditResult(
                success=True,
                diff_stat={"files": 1, "additions": 1, "deletions": 0},
                changed_paths=["src/theme.ts"],
                requirement_ledger=ledger,
                verification_plan=plan,
            )
        ),
        mint_token=_mint,
        open_pr=open_pr,
    )

    stored = await store.get_changeset(pool, "cs_noci0001")
    assert stored.status is ChangesetStatus.pr_open
    assert stored.external_ci_status is ExternalCIStatus.unverified_external_ci
    assert stored.github_pr_status is GitHubPRStatus.draft
    assert calls["draft"] is True


@pytest.mark.asyncio
async def test_job_rejects_pr_whose_head_differs_from_pushed_branch():
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_head0001")

    async def open_pr(**kwargs) -> PullRequest:
        return _pr(15, head_sha="different-head")

    await run_changeset_job(
        pool,
        "cs_head0001",
        editor=FakeEditor(),
        mint_token=_mint,
        open_pr=open_pr,
    )

    stored = await store.get_changeset(pool, "cs_head0001")
    assert stored.status is ChangesetStatus.error
    assert "exact branch head" in (stored.error or "")
    assert stored.pr_number is None
