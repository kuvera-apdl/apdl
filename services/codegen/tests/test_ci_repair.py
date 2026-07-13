"""Bounded, immutable CI repair on the exact GitHub pull-request head."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

import app.jobs.repair as repair_module
from app.editor.base import EditRequest, EditResult
from app.editor.fake import FakeEditor
from app.jobs.repair import repair_failed_ci as _repair_failed_ci
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
from app.runtime.models import (
    ArtifactFileEvidence,
    GeneratedRuntimeWorkflowAttestation,
    GeneratedRuntimeWorkflowExpectation,
    RequirementRuntimeEvidence,
    RuntimeAcceptancePlan,
    RuntimeArtifactObservation,
    RuntimeArtifactExpectation,
    RuntimeCheck,
    RuntimeCommand,
    RuntimeEvidenceAssessment,
    RuntimeEvidenceObservation,
    RuntimeEvidenceStatus,
    RuntimeEvidenceKind,
    RuntimeJobLogEvidence,
    RuntimeSurface,
    RuntimeAcceptanceRequest,
)
from app.safety.policy import (
    PlatformCodegenSafetyPolicy,
    TenantCodegenConnectionPolicy,
    TenantCodegenGatesPolicy,
)
from app.store import changesets as changeset_store
from app.store.observations import (
    apply_ci_verification_observation,
    apply_pull_request_observation,
    list_ci_remediation_attempts,
)
from app.store.runtime_evidence import apply_runtime_evidence_observation
from tests.fakes import FakePool
from tests.publication_fakes import (
    allowing_publication_gate,
    denying_publication_gate,
)


async def repair_failed_ci(*args, publication_gate=None, **kwargs):
    """Exercise repair with an explicit trusted publication gate."""
    return await _repair_failed_ci(
        *args,
        publication_gate=publication_gate or allowing_publication_gate(),
        **kwargs,
    )


async def _mint(_installation_id: int, _repo: str) -> str:
    return "ghs_tok"


def _repair_plan() -> RuntimeAcceptancePlan:
    return RuntimeAcceptancePlan(
        source_ledger_sha256="a" * 64,
        repo_profile_sha256="b" * 64,
        verification_plan_sha256="c" * 64,
    )


def _workflow_bound_repair_plan() -> RuntimeAcceptancePlan:
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
                    command="pytest -q", cwd=".", source_path="pyproject.toml"
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
    pool.store["changesets"]["cs-repair"]["runtime_acceptance_plan"] = (
        _repair_plan().model_dump(mode="json")
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


def _runtime_observation(
    *,
    head_sha: str,
    observed_at: datetime,
    identity: str,
    marker: str,
    ci_observation: CIVerificationObservation,
) -> RuntimeEvidenceObservation:
    artifact_text = f"{marker}: response payload did not contain the expected value"
    log_text = f"{marker}: assertion failed in browser acceptance test"
    return RuntimeEvidenceObservation(
        observation_id=f"runtime_obs_{identity * 32}",
        changeset_id="cs-repair",
        repository="acme/widgets",
        pr_number=7,
        head_sha=head_sha,
        ci_observation_id=ci_observation.observation_id,
        ci_evidence_hash=ci_observation.evidence_hash(),
        runtime_acceptance_plan_sha256=_repair_plan().evidence_hash(),
        observed_at=observed_at,
        artifacts=[
            RuntimeArtifactObservation(
                artifact_name="runtime_REQ-001_browser_report",
                artifact_id=41,
                workflow_run_id=31,
                head_sha=head_sha,
                status=RuntimeEvidenceStatus.observed,
                requirement_ids=["REQ-001"],
                files=[
                    ArtifactFileEvidence(
                        path="artifacts/browser-report.txt",
                        content_sha256=hashlib.sha256(
                            artifact_text.encode("utf-8")
                        ).hexdigest(),
                        byte_count=len(artifact_text.encode("utf-8")),
                        text_excerpt=artifact_text,
                    )
                ],
                github_url="https://github.com/acme/widgets/actions/runs/31",
            )
        ],
        job_logs=[
            RuntimeJobLogEvidence(
                workflow_run_id=31,
                job_id=32,
                job_name="runtime acceptance",
                head_sha=head_sha,
                text_excerpt=log_text,
                excerpt_byte_count=len(log_text.encode("utf-8")),
                source_byte_count=len(log_text.encode("utf-8")),
                truncated=False,
                redacted=True,
                github_url="https://github.com/acme/widgets/actions/runs/31/job/32",
            )
        ],
        assessment=RuntimeEvidenceAssessment(
            head_sha=head_sha,
            external_ci_status=ExternalCIStatus.failed,
            requirements=[
                RequirementRuntimeEvidence(
                    requirement_id="REQ-001",
                    status=RuntimeEvidenceStatus.observed,
                    artifact_names=["runtime_REQ-001_browser_report"],
                )
            ],
        ),
        collection_errors=[
            f"job_log:actions_read_failed:31:32: {marker}: bounded collection diagnostic"
        ],
    )


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
            diff_stat={"files": 1, "additions": 1, "deletions": 0},
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
async def test_repair_reuses_changeset_tenant_policy_snapshot(monkeypatch):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "2")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")
    pool, failed = await _seed_failed()
    strict_snapshot = TenantCodegenConnectionPolicy(
        gates=TenantCodegenGatesPolicy(max_files=1)
    )
    pool.store["changesets"]["cs-repair"]["tenant_policy_snapshot"] = (
        strict_snapshot.model_dump_json()
    )
    pool.store["connections"]["demo"]["tenant_policy"] = (
        TenantCodegenConnectionPolicy(
            gates=TenantCodegenGatesPolicy(max_files=50)
        ).model_dump_json()
    )
    editor = FakeEditor(
        EditResult(
            success=True,
            diff_stat={"files": 2, "additions": 2, "deletions": 0},
            changed_paths=["src/fix.py", "tests/test_fix.py"],
            diff_text="+fixed",
            head_sha="head-must-not-advance",
        )
    )

    await repair_failed_ci(pool, failed, editor=editor, mint_token=_mint)

    final = await changeset_store.get_changeset(pool, "cs-repair")
    assert editor.last_request is not None
    assert editor.last_request.safety_policy.max_files == 1
    assert final.head_sha == "head-failed"
    assert final.ci_remediation_status is CIRemediationStatus.exhausted
    attempts = await list_ci_remediation_attempts(pool, "cs-repair")
    assert any("1-file limit" in (attempt.error or "") for attempt in attempts)


@pytest.mark.asyncio
async def test_repair_rollout_denial_never_mints_token_or_invokes_editor(
    monkeypatch,
):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "2")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")
    pool, failed = await _seed_failed()
    editor = FakeEditor()
    minted: list[tuple[int, str]] = []

    async def mint(installation_id: int, repo: str) -> str:
        minted.append((installation_id, repo))
        return "must-not-be-returned"

    await repair_failed_ci(
        pool,
        failed,
        editor=editor,
        mint_token=mint,
        publication_gate=denying_publication_gate(),
    )

    final = await changeset_store.get_changeset(pool, "cs-repair")
    assert final is not None
    assert final.ci_remediation_status is CIRemediationStatus.exhausted
    assert final.publication_authorization is not None
    assert final.publication_authorization.decision.allowed is False
    assert minted == []
    assert editor.last_request is None


@pytest.mark.asyncio
async def test_forged_workflow_content_attestation_cannot_bypass_repair_gate(
    monkeypatch,
):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "2")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")
    pool, failed = await _seed_failed()
    workflow = ".github/workflows/apdl-runtime-acceptance.yml"
    pool.store["connections"]["demo"]["tenant_policy"] = (
        TenantCodegenConnectionPolicy(
            runtime_acceptance=RuntimeAcceptanceRequest(enabled=True)
        ).model_dump_json()
    )
    plan = _workflow_bound_repair_plan()
    pool.store["changesets"]["cs-repair"]["runtime_acceptance_plan"] = (
        plan.model_dump(mode="json")
    )
    editor = _CountingEditor(
        EditResult(
            success=True,
            diff_stat={"files": 1, "additions": 1, "deletions": 0},
            changed_paths=[workflow],
            diff_text=f"diff --git a/{workflow} b/{workflow}\n+permissions: write-all",
            head_sha="head-forged",
            runtime_acceptance_plan=plan,
            generated_runtime_workflow=GeneratedRuntimeWorkflowAttestation(
                path=workflow,
                content_sha256="e" * 64,
                runtime_acceptance_plan_sha256=plan.evidence_hash(),
            ),
        )
    )

    await repair_failed_ci(
        pool,
        failed,
        editor=editor,
        mint_token=_mint,
        platform_safety_policy=PlatformCodegenSafetyPolicy(
            runtime_workflow_generation_enabled=True
        ),
    )

    final = await changeset_store.get_changeset(pool, "cs-repair")
    assert final is not None
    assert final.head_sha == "head-failed"
    assert final.ci_remediation_status is CIRemediationStatus.exhausted
    attempts = await list_ci_remediation_attempts(pool, "cs-repair")
    assert any(
        "protected path" in (attempt.error or "") for attempt in attempts
    )


@pytest.mark.asyncio
async def test_repair_uses_latest_exact_head_runtime_evidence_with_event_provenance(
    monkeypatch,
):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "2")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")
    pool, failed = await _seed_failed(
        observation=_failed_observation(
            name="quality gate",
            summary="command failed without a GitHub annotation",
        )
    )
    older = _runtime_observation(
        head_sha=failed.head_sha,
        observed_at=failed.observed_at + timedelta(seconds=1),
        identity="a",
        marker="OLDER_RUNTIME_EVIDENCE",
        ci_observation=failed,
    )
    latest = _runtime_observation(
        head_sha=failed.head_sha,
        observed_at=failed.observed_at + timedelta(seconds=2),
        identity="b",
        marker="LATEST_RUNTIME_EVIDENCE",
        ci_observation=failed,
    )
    assert (await apply_runtime_evidence_observation(pool, older)).inserted is True
    assert (await apply_runtime_evidence_observation(pool, latest)).inserted is True
    editor = _CountingEditor(EditResult(success=False, error="still failing"))

    await repair_failed_ci(pool, failed, editor=editor, mint_token=_mint)

    assert len(editor.requests) == 1
    repair_spec = editor.requests[0].spec
    assert latest.observation_id in repair_spec
    assert latest.evidence_hash() in repair_spec
    assert "LATEST_RUNTIME_EVIDENCE: assertion failed" in repair_spec
    assert "LATEST_RUNTIME_EVIDENCE: response payload" in repair_spec
    assert "LATEST_RUNTIME_EVIDENCE: bounded collection diagnostic" in repair_spec
    assert "OLDER_RUNTIME_EVIDENCE" not in repair_spec
    assert len(repair_spec.encode("utf-8")) < 24_000

    events = await list_ci_remediation_attempts(
        pool, "cs-repair", failed_head_sha=failed.head_sha
    )
    assert {event.runtime_evidence_observation_id for event in events} == {
        latest.observation_id
    }
    assert {event.runtime_evidence_hash for event in events} == {
        latest.evidence_hash()
    }


@pytest.mark.asyncio
async def test_repair_excludes_runtime_evidence_from_a_stale_head(monkeypatch):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "2")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")
    pool, failed = await _seed_failed()
    stale = _runtime_observation(
        head_sha="head-stale",
        observed_at=failed.observed_at + timedelta(seconds=1),
        identity="c",
        marker="STALE_RUNTIME_EVIDENCE",
        ci_observation=failed,
    )
    stale_result = await apply_runtime_evidence_observation(pool, stale)
    assert stale_result.inserted is True
    assert stale_result.projected is False
    editor = _CountingEditor(EditResult(success=False, error="still failing"))

    await repair_failed_ci(pool, failed, editor=editor, mint_token=_mint)

    assert len(editor.requests) == 1
    repair_spec = editor.requests[0].spec
    assert "No exact-head GitHub runtime evidence" in repair_spec
    assert "STALE_RUNTIME_EVIDENCE" not in repair_spec
    events = await list_ci_remediation_attempts(
        pool, "cs-repair", failed_head_sha=failed.head_sha
    )
    assert all(event.runtime_evidence_observation_id is None for event in events)
    assert all(event.runtime_evidence_hash is None for event in events)


@pytest.mark.asyncio
async def test_runtime_evidence_read_failure_cannot_wedge_a_claim(monkeypatch):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "2")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")
    pool, failed = await _seed_failed()

    async def broken_lookup(*_args, **_kwargs):
        raise RuntimeError("runtime journal unavailable")

    monkeypatch.setattr(
        repair_module, "latest_runtime_evidence_observation", broken_lookup
    )
    editor = _CountingEditor(EditResult(success=False, error="still failing"))

    await repair_failed_ci(pool, failed, editor=editor, mint_token=_mint)

    assert len(editor.requests) == 1
    final = await changeset_store.get_changeset(pool, "cs-repair")
    assert final is not None
    assert final.ci_remediation_status is CIRemediationStatus.exhausted
    events = await list_ci_remediation_attempts(pool, "cs-repair")
    assert {event.event_sequence for event in events} == {1, 2}


@pytest.mark.asyncio
async def test_repeated_delivery_of_same_failure_cannot_launch_duplicate_repair(
    monkeypatch,
):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "3")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")
    pool, failed = await _seed_failed()
    editor = _CountingEditor(
        EditResult(
            success=False,
            error="agent could not repair",
            runtime_acceptance_plan=RuntimeAcceptancePlan(
                source_ledger_sha256="d" * 64,
                repo_profile_sha256="e" * 64,
                verification_plan_sha256="f" * 64,
            ),
        )
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
    assert final.runtime_acceptance_plan == _repair_plan()
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
                diff_stat={"files": 1, "additions": 1, "deletions": 0},
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
