"""Bounded exact-head GitHub runtime evidence collection tests."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from app.github.actions import ActionsJob, ActionsJobLog, ActionsWorkflowRun
from app.github.artifacts import GitHubArtifact
from app.runtime.collector import (
    RuntimeEvidenceCollection,
    collect_runtime_evidence,
)
from app.runtime.models import (
    ArtifactFileEvidence,
    RuntimeAcceptancePlan,
    RuntimeArtifactExpectation,
    RuntimeArtifactObservation,
    RuntimeCheck,
    RuntimeCommand,
    RuntimeEvidenceStatus,
    RuntimeEvidenceKind,
    RuntimeSurface,
)


def _plan() -> RuntimeAcceptancePlan:
    return RuntimeAcceptancePlan(
        source_ledger_sha256="a" * 64,
        repo_profile_sha256="b" * 64,
        verification_plan_sha256="c" * 64,
        repo="acme/widgets",
        branch="main",
        checks=[
            RuntimeCheck(
                check_id="runtime_aaaaaaaaaaaaaaaa",
                surface=RuntimeSurface.runtime,
                requirement_ids=["REQ-001", "REQ-002"],
                command=RuntimeCommand(
                    command="npm run test:runtime",
                    cwd=".",
                    source_path="package.json",
                ),
                expected_artifacts=[
                    RuntimeArtifactExpectation(
                        artifact_name="apdl-a-evidence",
                        evidence_kind=RuntimeEvidenceKind.structured_runtime,
                        paths=["a/**"],
                        requirement_ids=["REQ-001"],
                    ),
                    RuntimeArtifactExpectation(
                        artifact_name="apdl-b-evidence",
                        evidence_kind=RuntimeEvidenceKind.structured_runtime,
                        paths=["b/**"],
                        requirement_ids=["REQ-002"],
                    ),
                ],
            )
        ],
    )


def _run(
    run_id: int,
    *,
    head_sha: str = "head-new",
    status: str = "completed",
    attempt: int = 1,
) -> ActionsWorkflowRun:
    return ActionsWorkflowRun(
        run_id=run_id,
        name="APDL Runtime Acceptance",
        head_sha=head_sha,
        status=status,
        conclusion="failure" if status == "completed" else None,
        run_attempt=attempt,
        html_url=f"https://github.test/actions/runs/{run_id}",
    )


def _job(
    job_id: int,
    *,
    conclusion: str,
    head_sha: str = "head-new",
    workflow_run_id: int = 9,
) -> ActionsJob:
    return ActionsJob(
        job_id=job_id,
        workflow_run_id=workflow_run_id,
        head_sha=head_sha,
        name=f"runtime-{job_id}",
        status="completed",
        conclusion=conclusion,
        html_url=f"https://github.test/actions/jobs/{job_id}",
    )


def _artifact(
    artifact_id: int,
    name: str,
    *,
    head_sha: str = "head-new",
    workflow_run_id: int = 9,
) -> GitHubArtifact:
    return GitHubArtifact(
        artifact_id=artifact_id,
        workflow_run_id=workflow_run_id,
        head_sha=head_sha,
        name=name,
        size_in_bytes=10,
        archive_download_url=f"https://api.github.test/artifacts/{artifact_id}",
    )


def _forbidden(stage: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://api.github.test/{stage}")
    response = httpx.Response(403, request=request)
    return httpx.HTTPStatusError("forbidden", request=request, response=response)


@pytest.mark.asyncio
async def test_collector_selects_latest_exact_head_and_binds_bounded_evidence():
    calls: dict[str, list[int]] = {"jobs": [], "logs": [], "artifacts": []}

    async def list_runs(repo: str, head_sha: str, token: str):
        assert (repo, head_sha, token) == ("acme/widgets", "head-new", "tok")
        return [
            _run(99, head_sha="head-old"),
            _run(9, attempt=1),
            _run(8),
            _run(9, attempt=2),
        ]

    async def list_jobs(repo: str, run_id: int, head_sha: str, token: str):
        assert (repo, head_sha, token) == ("acme/widgets", "head-new", "tok")
        calls["jobs"].append(run_id)
        return [
            _job(91, conclusion="success"),
            _job(92, conclusion="failure"),
            _job(90, conclusion="timed_out"),
            _job(92, conclusion="failure"),
            _job(89, conclusion="failure", head_sha="head-old"),
        ]

    async def read_log(
        repo: str,
        job_id: int,
        head_sha: str,
        token: str,
        *,
        max_bytes: int,
    ):
        assert (repo, head_sha, token, max_bytes) == (
            "acme/widgets",
            "head-new",
            "tok",
            31,
        )
        calls["logs"].append(job_id)
        return ActionsJobLog(
            job_id=job_id,
            workflow_run_id=9,
            head_sha="head-new",
            text="é" * 100,
            byte_count=200,
            truncated=False,
            redacted=True,
        )

    async def list_artifacts(repo: str, run_id: int, head_sha: str, token: str):
        assert (repo, head_sha, token) == ("acme/widgets", "head-new", "tok")
        calls["artifacts"].append(run_id)
        return [
            _artifact(91, "apdl-a-evidence"),
            _artifact(92, "apdl-a-evidence"),
            _artifact(93, "apdl-b-evidence", head_sha="head-old"),
            _artifact(94, "unplanned-evidence"),
        ]

    async def download_artifact(
        artifact: GitHubArtifact,
        expectation: RuntimeArtifactExpectation,
        token: str,
    ):
        assert artifact.artifact_id == 92
        assert expectation.artifact_name == "apdl-a-evidence"
        assert token == "tok"
        return RuntimeArtifactObservation(
            artifact_name=expectation.artifact_name,
            artifact_id=artifact.artifact_id,
            workflow_run_id=artifact.workflow_run_id,
            head_sha=artifact.head_sha,
            status=RuntimeEvidenceStatus.observed,
            requirement_ids=expectation.requirement_ids,
            files=[
                ArtifactFileEvidence(
                    path="a/result.json",
                    content_sha256="a" * 64,
                    byte_count=2,
                    text_excerpt="{}",
                )
            ],
            github_url=artifact.archive_download_url,
        )

    result = await collect_runtime_evidence(
        "acme/widgets",
        "head-new",
        "tok",
        _plan(),
        list_runs_fn=list_runs,
        list_jobs_fn=list_jobs,
        read_log_fn=read_log,
        list_artifacts_fn=list_artifacts,
        download_artifact_fn=download_artifact,
        max_runs=1,
        max_jobs=2,
        max_logs=1,
        max_artifacts=2,
        max_log_bytes=31,
    )

    assert calls == {"jobs": [9], "logs": [92], "artifacts": [9]}
    assert [(item.workflow_run_id, item.job_id) for item in result.job_logs] == [
        (9, 92)
    ]
    assert result.job_logs[0].excerpt_byte_count == 30
    assert result.job_logs[0].truncated is True
    assert result.job_logs[0].redacted is True
    assert [item.artifact_name for item in result.artifacts] == [
        "apdl-a-evidence",
        "apdl-b-evidence",
    ]
    assert result.artifacts[0].artifact_id == 92
    assert result.artifacts[0].status is RuntimeEvidenceStatus.observed
    assert result.artifacts[1].status is RuntimeEvidenceStatus.unverified
    assert "not present" in (result.artifacts[1].unverified_reason or "")
    assert {item.code for item in result.diagnostics} >= {
        "collection_truncated",
        "stale_actions_resource",
    }
    assert "external_ci_status" not in result.model_dump()


@pytest.mark.asyncio
async def test_collector_reapplies_canonical_redaction_to_injected_adapters():
    log_secret = "Authorization: Bearer opaque-bearer-value"
    artifact_secret = "DATABASE_URL=postgresql://user:password@db.internal/app"

    async def list_runs(repo: str, head_sha: str, token: str):
        return [_run(9)]

    async def list_jobs(repo: str, run_id: int, head_sha: str, token: str):
        return [_job(92, conclusion="failure")]

    async def read_log(
        repo: str,
        job_id: int,
        head_sha: str,
        token: str,
        *,
        max_bytes: int,
    ):
        return ActionsJobLog(
            job_id=job_id,
            workflow_run_id=9,
            head_sha=head_sha,
            text=log_secret,
            byte_count=len(log_secret.encode()),
            redacted=False,
        )

    async def list_artifacts(repo: str, run_id: int, head_sha: str, token: str):
        return [_artifact(92, "apdl-a-evidence")]

    async def download_artifact(
        artifact: GitHubArtifact,
        expectation: RuntimeArtifactExpectation,
        token: str,
    ):
        return RuntimeArtifactObservation(
            artifact_name=expectation.artifact_name,
            artifact_id=artifact.artifact_id,
            workflow_run_id=artifact.workflow_run_id,
            head_sha=artifact.head_sha,
            status=RuntimeEvidenceStatus.observed,
            requirement_ids=expectation.requirement_ids,
            files=[
                ArtifactFileEvidence(
                    path="a/result.txt",
                    content_sha256="a" * 64,
                    byte_count=len(artifact_secret.encode()),
                    text_excerpt=artifact_secret,
                    redacted=False,
                )
            ],
        )

    result = await collect_runtime_evidence(
        "acme/widgets",
        "head-new",
        "tok",
        _plan(),
        list_runs_fn=list_runs,
        list_jobs_fn=list_jobs,
        read_log_fn=read_log,
        list_artifacts_fn=list_artifacts,
        download_artifact_fn=download_artifact,
        max_artifacts=1,
    )

    assert result.job_logs[0].redacted is True
    assert log_secret not in result.job_logs[0].text_excerpt
    assert result.artifacts[0].files[0].redacted is True
    assert artifact_secret not in (result.artifacts[0].files[0].text_excerpt or "")


@pytest.mark.asyncio
async def test_workflow_read_403_is_unverified_diagnostic_and_never_raises():
    async def list_runs(repo: str, head_sha: str, token: str):
        raise _forbidden("runs")

    result = await collect_runtime_evidence(
        "acme/widgets",
        "head-new",
        "tok",
        _plan(),
        list_runs_fn=list_runs,
    )

    assert result.job_logs == []
    assert result.artifacts == []
    assert [(item.code, item.status) for item in result.diagnostics] == [
        ("actions_read_forbidden", "unverified")
    ]
    with pytest.raises(ValidationError):
        RuntimeEvidenceCollection.model_validate(
            {**result.model_dump(), "external_ci_status": "passed"}
        )


@pytest.mark.asyncio
async def test_missing_exact_head_runs_are_explicitly_unverified():
    async def list_runs(repo: str, head_sha: str, token: str):
        return []

    result = await collect_runtime_evidence(
        "acme/widgets",
        "head-new",
        "tok",
        _plan(),
        list_runs_fn=list_runs,
    )

    assert result.job_logs == []
    assert result.artifacts == []
    assert [item.code for item in result.diagnostics] == ["workflow_runs_missing"]
    assert result.diagnostics[0].status == "unverified"


@pytest.mark.asyncio
async def test_completed_run_artifact_403_produces_unverified_observations():
    async def list_runs(repo: str, head_sha: str, token: str):
        return [_run(9)]

    async def list_jobs(repo: str, run_id: int, head_sha: str, token: str):
        return []

    async def list_artifacts(repo: str, run_id: int, head_sha: str, token: str):
        raise _forbidden("artifacts")

    async def download_artifact(
        artifact: GitHubArtifact,
        expectation: RuntimeArtifactExpectation,
        token: str,
    ):
        raise AssertionError("no artifact metadata was available")

    result = await collect_runtime_evidence(
        "acme/widgets",
        "head-new",
        "tok",
        _plan(),
        list_runs_fn=list_runs,
        list_jobs_fn=list_jobs,
        list_artifacts_fn=list_artifacts,
        download_artifact_fn=download_artifact,
        max_artifacts=1,
    )

    assert [item.artifact_name for item in result.artifacts] == ["apdl-a-evidence"]
    assert result.artifacts[0].status is RuntimeEvidenceStatus.unverified
    assert "could not be read" in (result.artifacts[0].unverified_reason or "")
    assert {(item.stage, item.code) for item in result.diagnostics} == {
        ("artifacts", "actions_read_forbidden"),
        ("artifacts", "collection_truncated"),
    }


@pytest.mark.asyncio
async def test_failed_artifact_download_redacts_fallback_metadata():
    secret_url = "https://api.github.test/artifacts/92?access_token=top-secret-value"

    async def list_runs(repo: str, head_sha: str, token: str):
        return [_run(9)]

    async def list_jobs(repo: str, run_id: int, head_sha: str, token: str):
        return []

    async def list_artifacts(repo: str, run_id: int, head_sha: str, token: str):
        return [
            _artifact(92, "apdl-a-evidence").model_copy(
                update={"archive_download_url": secret_url}
            )
        ]

    async def download_artifact(
        artifact: GitHubArtifact,
        expectation: RuntimeArtifactExpectation,
        token: str,
    ):
        raise ValueError("injected download failure")

    result = await collect_runtime_evidence(
        "acme/widgets",
        "head-new",
        "tok",
        _plan(),
        list_runs_fn=list_runs,
        list_jobs_fn=list_jobs,
        list_artifacts_fn=list_artifacts,
        download_artifact_fn=download_artifact,
        max_artifacts=1,
    )

    assert result.artifacts[0].status is RuntimeEvidenceStatus.unverified
    assert "top-secret-value" not in (result.artifacts[0].github_url or "")
    assert "[REDACTED]" in (result.artifacts[0].github_url or "")


@pytest.mark.asyncio
async def test_incomplete_runs_do_not_prematurely_claim_artifacts_are_missing():
    async def list_runs(repo: str, head_sha: str, token: str):
        return [_run(9, status="in_progress")]

    async def list_jobs(repo: str, run_id: int, head_sha: str, token: str):
        return []

    async def list_artifacts(repo: str, run_id: int, head_sha: str, token: str):
        return []

    result = await collect_runtime_evidence(
        "acme/widgets",
        "head-new",
        "tok",
        _plan(),
        list_runs_fn=list_runs,
        list_jobs_fn=list_jobs,
        list_artifacts_fn=list_artifacts,
    )

    assert result.artifacts == []
    assert [item.code for item in result.diagnostics] == ["workflow_runs_incomplete"]
