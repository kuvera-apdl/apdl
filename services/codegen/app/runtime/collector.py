"""Collect bounded, exact-head runtime evidence from GitHub Actions.

This module deliberately returns evidence and diagnostics only.  GitHub's
external CI observation remains authoritative and is never projected or
reclassified by runtime-artifact availability.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.github.actions import (
    ActionsJob,
    ActionsJobLog,
    ActionsWorkflowRun,
    list_run_jobs,
    list_workflow_runs,
    read_job_log,
)
from app.github.artifacts import (
    GitHubArtifact,
    StaleActionsHeadError,
    download_artifact_observation,
    list_run_artifacts,
    missing_artifact_observation,
)
from app.runtime.models import (
    RuntimeAcceptancePlan,
    RuntimeArtifactExpectation,
    RuntimeArtifactObservation,
    RuntimeEvidenceStatus,
    RuntimeJobLogEvidence,
)
from app.safety.secrets import contains_secret, redact_secrets

_HEAD_SHA_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"
_FAILED_JOB_CONCLUSIONS = frozenset(
    {
        "action_required",
        "cancelled",
        "failure",
        "stale",
        "startup_failure",
        "timed_out",
    }
)

ListRuns = Callable[[str, str, str], Awaitable[list[ActionsWorkflowRun]]]
ListJobs = Callable[[str, int, str, str], Awaitable[list[ActionsJob]]]
ReadLog = Callable[..., Awaitable[ActionsJobLog]]
ListArtifacts = Callable[[str, int, str, str], Awaitable[list[GitHubArtifact]]]
DownloadArtifact = Callable[
    [GitHubArtifact, RuntimeArtifactExpectation, str],
    Awaitable[RuntimeArtifactObservation],
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeCollectionDiagnostic(StrictModel):
    """An explicit reason runtime evidence remains unverified."""

    schema_version: Literal["runtime_collection_diagnostic@1"] = (
        "runtime_collection_diagnostic@1"
    )
    status: Literal["unverified"] = "unverified"
    code: Literal[
        "actions_read_forbidden",
        "actions_read_failed",
        "collection_truncated",
        "stale_actions_resource",
        "workflow_runs_incomplete",
        "workflow_runs_missing",
    ]
    stage: Literal[
        "artifact_download",
        "artifacts",
        "collector",
        "job_log",
        "jobs",
        "workflow_runs",
    ]
    head_sha: str = Field(pattern=_HEAD_SHA_PATTERN)
    message: str = Field(min_length=1, max_length=2000)
    workflow_run_id: int | None = Field(default=None, ge=1)
    job_id: int | None = Field(default=None, ge=1)
    artifact_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,128}$")


def _diagnostic_key(item: RuntimeCollectionDiagnostic) -> tuple:
    return (
        item.stage,
        item.workflow_run_id or 0,
        item.job_id or 0,
        item.artifact_name or "",
        item.code,
        item.message,
    )


class RuntimeEvidenceCollection(StrictModel):
    """Bounded evidence payload with no field capable of changing CI state."""

    schema_version: Literal["runtime_evidence_collection@1"] = (
        "runtime_evidence_collection@1"
    )
    head_sha: str = Field(pattern=_HEAD_SHA_PATTERN)
    job_logs: list[RuntimeJobLogEvidence] = Field(default_factory=list)
    artifacts: list[RuntimeArtifactObservation] = Field(default_factory=list)
    diagnostics: list[RuntimeCollectionDiagnostic] = Field(default_factory=list)

    @field_validator("job_logs")
    @classmethod
    def validate_job_logs(
        cls, values: list[RuntimeJobLogEvidence]
    ) -> list[RuntimeJobLogEvidence]:
        keys = [(value.workflow_run_id, value.job_id) for value in values]
        if keys != sorted(set(keys)):
            raise ValueError("runtime job logs must be sorted and unique")
        return values

    @field_validator("artifacts")
    @classmethod
    def validate_artifacts(
        cls, values: list[RuntimeArtifactObservation]
    ) -> list[RuntimeArtifactObservation]:
        keys = [value.artifact_name for value in values]
        if keys != sorted(set(keys)):
            raise ValueError("runtime artifact observations must be sorted and unique")
        return values

    @field_validator("diagnostics")
    @classmethod
    def validate_diagnostics(
        cls, values: list[RuntimeCollectionDiagnostic]
    ) -> list[RuntimeCollectionDiagnostic]:
        keys = [_diagnostic_key(value) for value in values]
        if keys != sorted(set(keys)):
            raise ValueError("runtime collection diagnostics must be sorted and unique")
        return values


def _diagnostic(
    *,
    code: Literal[
        "actions_read_forbidden",
        "actions_read_failed",
        "collection_truncated",
        "stale_actions_resource",
        "workflow_runs_incomplete",
        "workflow_runs_missing",
    ],
    stage: Literal[
        "artifact_download",
        "artifacts",
        "collector",
        "job_log",
        "jobs",
        "workflow_runs",
    ],
    head_sha: str,
    message: str,
    workflow_run_id: int | None = None,
    job_id: int | None = None,
    artifact_name: str | None = None,
) -> RuntimeCollectionDiagnostic:
    return RuntimeCollectionDiagnostic(
        code=code,
        stage=stage,
        head_sha=head_sha,
        message=message,
        workflow_run_id=workflow_run_id,
        job_id=job_id,
        artifact_name=artifact_name,
    )


def _exception_diagnostic(
    error: httpx.HTTPError | ValueError,
    *,
    stage: Literal[
        "artifact_download", "artifacts", "job_log", "jobs", "workflow_runs"
    ],
    head_sha: str,
    workflow_run_id: int | None = None,
    job_id: int | None = None,
    artifact_name: str | None = None,
) -> RuntimeCollectionDiagnostic:
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 403:
        code: Literal[
            "actions_read_forbidden", "actions_read_failed", "stale_actions_resource"
        ] = "actions_read_forbidden"
        message = (
            f"GitHub Actions {stage} read was forbidden (HTTP 403); "
            "runtime evidence is unverified."
        )
    elif isinstance(error, StaleActionsHeadError):
        code = "stale_actions_resource"
        message = (
            f"GitHub Actions {stage} returned a resource outside the requested "
            "exact head; runtime evidence is unverified."
        )
    elif isinstance(error, httpx.HTTPStatusError):
        code = "actions_read_failed"
        message = (
            f"GitHub Actions {stage} read failed with HTTP "
            f"{error.response.status_code}; runtime evidence is unverified."
        )
    elif isinstance(error, httpx.RequestError):
        code = "actions_read_failed"
        message = (
            f"GitHub Actions {stage} read failed due to a request error; "
            "runtime evidence is unverified."
        )
    else:
        code = "actions_read_failed"
        message = (
            f"GitHub Actions {stage} returned unusable evidence; "
            "runtime evidence is unverified."
        )
    return _diagnostic(
        code=code,
        stage=stage,
        head_sha=head_sha,
        message=message,
        workflow_run_id=workflow_run_id,
        job_id=job_id,
        artifact_name=artifact_name,
    )


def _result(
    head_sha: str,
    *,
    job_logs: list[RuntimeJobLogEvidence] | None = None,
    artifacts: list[RuntimeArtifactObservation] | None = None,
    diagnostics: list[RuntimeCollectionDiagnostic] | None = None,
) -> RuntimeEvidenceCollection:
    unique_diagnostics = {_diagnostic_key(item): item for item in (diagnostics or [])}
    return RuntimeEvidenceCollection(
        head_sha=head_sha,
        job_logs=sorted(
            job_logs or [], key=lambda item: (item.workflow_run_id, item.job_id)
        ),
        artifacts=sorted(artifacts or [], key=lambda item: item.artifact_name),
        diagnostics=sorted(unique_diagnostics.values(), key=_diagnostic_key),
    )


def _select_runs(
    values: list[ActionsWorkflowRun], head_sha: str
) -> list[ActionsWorkflowRun]:
    """Choose the highest attempt for every exact-head workflow-run ID."""
    selected: dict[int, ActionsWorkflowRun] = {}
    for item in sorted(
        values,
        key=lambda value: (value.run_id, value.run_attempt, value.model_dump_json()),
    ):
        if item.head_sha == head_sha:
            selected[item.run_id] = item
    return sorted(
        selected.values(),
        key=lambda value: (value.run_id, value.run_attempt),
        reverse=True,
    )


def _failed_job(job: ActionsJob) -> bool:
    conclusion = (job.conclusion or "").lower()
    return job.status.lower() == "completed" and conclusion in _FAILED_JOB_CONCLUSIONS


def _utf8_prefix(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", "ignore"), True


def redact_artifact_observation(
    observation: RuntimeArtifactObservation,
) -> RuntimeArtifactObservation:
    """Reapply canonical redaction at the collector's injected-adapter boundary."""
    if contains_secret(observation.artifact_name):
        raise ValueError("runtime artifact name contains secret material")
    files = []
    for file in observation.files:
        if contains_secret(file.path):
            raise ValueError("runtime artifact path contains secret material")
        excerpt = file.text_excerpt
        redacted = file.redacted
        if excerpt is not None:
            excerpt, changed = redact_secrets(excerpt)
            excerpt = excerpt[:8000]
            redacted = redacted or changed
        files.append(
            file.model_copy(
                update={"text_excerpt": excerpt, "redacted": redacted},
                deep=True,
            )
        )
    github_url = observation.github_url
    if github_url is not None:
        github_url = redact_secrets(github_url)[0][:2000]
    reason = observation.unverified_reason
    if reason is not None:
        reason = redact_secrets(reason)[0][:2000]
    return RuntimeArtifactObservation.model_validate(
        {
            **observation.model_dump(mode="python"),
            "files": [file.model_dump(mode="python") for file in files],
            "github_url": github_url,
            "unverified_reason": reason,
        }
    )


def _unverified_artifact(
    expectation: RuntimeArtifactExpectation,
    *,
    workflow_run_id: int,
    head_sha: str,
    reason: str,
    artifact: GitHubArtifact | None = None,
) -> RuntimeArtifactObservation:
    github_url = None
    if artifact is not None:
        github_url = redact_secrets(artifact.archive_download_url)[0][:2000]
    return RuntimeArtifactObservation(
        artifact_name=expectation.artifact_name,
        artifact_id=artifact.artifact_id if artifact is not None else None,
        workflow_run_id=workflow_run_id,
        head_sha=head_sha,
        status=RuntimeEvidenceStatus.unverified,
        requirement_ids=expectation.requirement_ids,
        files=[],
        github_url=github_url,
        unverified_reason=reason,
    )


async def collect_runtime_evidence(
    repo: str,
    head_sha: str,
    token: str,
    plan: RuntimeAcceptancePlan,
    *,
    list_runs_fn: ListRuns = list_workflow_runs,
    list_jobs_fn: ListJobs = list_run_jobs,
    read_log_fn: ReadLog = read_job_log,
    list_artifacts_fn: ListArtifacts = list_run_artifacts,
    download_artifact_fn: DownloadArtifact = download_artifact_observation,
    max_runs: int = 10,
    max_jobs: int = 100,
    max_logs: int = 20,
    max_artifacts: int = 50,
    max_log_bytes: int = 8000,
) -> RuntimeEvidenceCollection:
    """Collect exact-head failure logs and expected artifacts without judging CI."""
    if min(max_runs, max_jobs, max_logs, max_artifacts, max_log_bytes) <= 0:
        raise ValueError("runtime evidence collection budgets must be positive")
    if max_log_bytes > 8000:
        raise ValueError("max_log_bytes cannot exceed the evidence contract's limit")
    if plan.repo is not None and plan.repo != repo:
        raise ValueError("runtime plan repository does not match the requested repo")

    diagnostics: list[RuntimeCollectionDiagnostic] = []
    try:
        raw_runs = await list_runs_fn(repo, head_sha, token)
    except (httpx.HTTPError, ValueError) as error:
        diagnostics.append(
            _exception_diagnostic(error, stage="workflow_runs", head_sha=head_sha)
        )
        return _result(head_sha, diagnostics=diagnostics)

    runs = _select_runs(raw_runs, head_sha)
    stale_count = sum(item.head_sha != head_sha for item in raw_runs)
    if stale_count:
        diagnostics.append(
            _diagnostic(
                code="stale_actions_resource",
                stage="workflow_runs",
                head_sha=head_sha,
                message=(
                    f"Ignored {stale_count} workflow run(s) outside the requested "
                    "exact head."
                ),
            )
        )
    if not runs:
        diagnostics.append(
            _diagnostic(
                code="workflow_runs_missing",
                stage="workflow_runs",
                head_sha=head_sha,
                message=(
                    "GitHub returned no Actions workflow runs for the requested "
                    "exact head; runtime evidence is unverified."
                ),
            )
        )
        return _result(head_sha, diagnostics=diagnostics)
    if len(runs) > max_runs:
        diagnostics.append(
            _diagnostic(
                code="collection_truncated",
                stage="workflow_runs",
                head_sha=head_sha,
                message=f"Workflow-run collection was capped at {max_runs} runs.",
            )
        )
        runs = runs[:max_runs]

    run_by_id = {run.run_id: run for run in runs}

    jobs_by_key: dict[tuple[int, int], ActionsJob] = {}
    for run in runs:
        try:
            run_jobs = await list_jobs_fn(repo, run.run_id, head_sha, token)
        except (httpx.HTTPError, ValueError) as error:
            diagnostics.append(
                _exception_diagnostic(
                    error,
                    stage="jobs",
                    head_sha=head_sha,
                    workflow_run_id=run.run_id,
                )
            )
            continue
        stale_jobs = 0
        for job in run_jobs:
            if job.head_sha != head_sha or job.workflow_run_id != run.run_id:
                stale_jobs += 1
                continue
            key = (job.workflow_run_id, job.job_id)
            current = jobs_by_key.get(key)
            if current is None or job.model_dump_json() > current.model_dump_json():
                jobs_by_key[key] = job
        if stale_jobs:
            diagnostics.append(
                _diagnostic(
                    code="stale_actions_resource",
                    stage="jobs",
                    head_sha=head_sha,
                    workflow_run_id=run.run_id,
                    message=(
                        f"Ignored {stale_jobs} job(s) outside workflow run "
                        f"{run.run_id} and the requested exact head."
                    ),
                )
            )

    jobs = sorted(
        jobs_by_key.values(),
        key=lambda job: (not _failed_job(job), -job.workflow_run_id, -job.job_id),
    )
    if len(jobs) > max_jobs:
        diagnostics.append(
            _diagnostic(
                code="collection_truncated",
                stage="jobs",
                head_sha=head_sha,
                message=f"Job collection was capped at {max_jobs} jobs.",
            )
        )
        jobs = jobs[:max_jobs]

    failed_jobs = [job for job in jobs if _failed_job(job)]
    if len(failed_jobs) > max_logs:
        diagnostics.append(
            _diagnostic(
                code="collection_truncated",
                stage="job_log",
                head_sha=head_sha,
                message=f"Failed-job log collection was capped at {max_logs} logs.",
            )
        )
        failed_jobs = failed_jobs[:max_logs]

    job_logs: list[RuntimeJobLogEvidence] = []
    for job in failed_jobs:
        try:
            log = await read_log_fn(
                repo,
                job.job_id,
                head_sha,
                token,
                max_bytes=max_log_bytes,
            )
            inspected, source_bounded = _utf8_prefix(
                log.text,
                max_log_bytes + 512,
            )
            inspected, canonical_redacted = redact_secrets(inspected)
        except (httpx.HTTPError, ValueError) as error:
            diagnostics.append(
                _exception_diagnostic(
                    error,
                    stage="job_log",
                    head_sha=head_sha,
                    workflow_run_id=job.workflow_run_id,
                    job_id=job.job_id,
                )
            )
            continue
        if (
            log.head_sha != head_sha
            or log.workflow_run_id != job.workflow_run_id
            or log.job_id != job.job_id
        ):
            diagnostics.append(
                _diagnostic(
                    code="stale_actions_resource",
                    stage="job_log",
                    head_sha=head_sha,
                    workflow_run_id=job.workflow_run_id,
                    job_id=job.job_id,
                    message=(
                        "Ignored a job log that did not match its exact-head job "
                        "identity."
                    ),
                )
            )
            continue
        excerpt, bounded = _utf8_prefix(inspected, max_log_bytes)
        excerpt_bytes = len(excerpt.encode("utf-8"))
        github_url = (
            job.html_url
            or f"https://github.com/{repo}/actions/runs/"
            f"{job.workflow_run_id}/job/{job.job_id}"
        )
        github_url = redact_secrets(github_url)[0][:2000]
        job_name = redact_secrets(job.name)[0][:300]
        job_logs.append(
            RuntimeJobLogEvidence(
                workflow_run_id=job.workflow_run_id,
                job_id=job.job_id,
                job_name=job_name or "job",
                head_sha=head_sha,
                text_excerpt=excerpt,
                excerpt_byte_count=excerpt_bytes,
                source_byte_count=log.byte_count,
                truncated=log.truncated or source_bounded or bounded,
                redacted=log.redacted or canonical_redacted,
                github_url=github_url,
            )
        )

    expectations = sorted(
        (
            expectation
            for check in plan.checks
            for expectation in check.expected_artifacts
        ),
        key=lambda item: item.artifact_name,
    )
    if len(expectations) > max_artifacts:
        diagnostics.append(
            _diagnostic(
                code="collection_truncated",
                stage="artifacts",
                head_sha=head_sha,
                message=(
                    "Runtime artifact expectation collection was capped at "
                    f"{max_artifacts} artifacts."
                ),
            )
        )
        expectations = expectations[:max_artifacts]

    expected_names = {item.artifact_name for item in expectations}
    artifacts_by_name: dict[str, GitHubArtifact] = {}
    artifact_read_failures: dict[int, RuntimeCollectionDiagnostic] = {}
    if expectations:
        for run in runs:
            try:
                run_artifacts = await list_artifacts_fn(
                    repo, run.run_id, head_sha, token
                )
            except (httpx.HTTPError, ValueError) as error:
                diagnostic = _exception_diagnostic(
                    error,
                    stage="artifacts",
                    head_sha=head_sha,
                    workflow_run_id=run.run_id,
                )
                diagnostics.append(diagnostic)
                artifact_read_failures[run.run_id] = diagnostic
                continue
            stale_artifacts = 0
            for artifact in run_artifacts:
                if (
                    artifact.head_sha != head_sha
                    or artifact.workflow_run_id != run.run_id
                ):
                    stale_artifacts += 1
                    continue
                if artifact.name not in expected_names:
                    continue
                current = artifacts_by_name.get(artifact.name)
                candidate_key = (
                    artifact.workflow_run_id,
                    run.run_attempt,
                    artifact.artifact_id,
                )
                if current is None:
                    artifacts_by_name[artifact.name] = artifact
                    continue
                current_run = run_by_id[current.workflow_run_id]
                current_key = (
                    current.workflow_run_id,
                    current_run.run_attempt,
                    current.artifact_id,
                )
                if candidate_key > current_key:
                    artifacts_by_name[artifact.name] = artifact
            if stale_artifacts:
                diagnostics.append(
                    _diagnostic(
                        code="stale_actions_resource",
                        stage="artifacts",
                        head_sha=head_sha,
                        workflow_run_id=run.run_id,
                        message=(
                            f"Ignored {stale_artifacts} artifact(s) outside workflow "
                            f"run {run.run_id} and the requested exact head."
                        ),
                    )
                )

    completed_runs = [run for run in runs if run.status.lower() == "completed"]
    completed_fallback = completed_runs[0] if completed_runs else None
    observations: list[RuntimeArtifactObservation] = []
    for expectation in expectations:
        artifact = artifacts_by_name.get(expectation.artifact_name)
        if artifact is None:
            if completed_fallback is None:
                continue
            failure = artifact_read_failures.get(completed_fallback.run_id)
            if failure is not None:
                observations.append(
                    _unverified_artifact(
                        expectation,
                        workflow_run_id=completed_fallback.run_id,
                        head_sha=head_sha,
                        reason=(
                            "GitHub Actions artifact metadata could not be read for "
                            "the completed exact-head workflow run."
                        ),
                    )
                )
            else:
                observations.append(
                    redact_artifact_observation(
                        missing_artifact_observation(
                            expectation,
                            workflow_run_id=completed_fallback.run_id,
                            head_sha=head_sha,
                            github_url=completed_fallback.html_url,
                        )
                    )
                )
            continue
        try:
            observation = await download_artifact_fn(artifact, expectation, token)
            observation = redact_artifact_observation(observation)
        except (httpx.HTTPError, ValueError) as error:
            diagnostics.append(
                _exception_diagnostic(
                    error,
                    stage="artifact_download",
                    head_sha=head_sha,
                    workflow_run_id=artifact.workflow_run_id,
                    artifact_name=expectation.artifact_name,
                )
            )
            observations.append(
                _unverified_artifact(
                    expectation,
                    workflow_run_id=artifact.workflow_run_id,
                    head_sha=head_sha,
                    artifact=artifact,
                    reason=(
                        "The expected exact-head GitHub Actions artifact could not "
                        "be downloaded or inspected."
                    ),
                )
            )
            continue
        if (
            observation.head_sha != head_sha
            or observation.workflow_run_id != artifact.workflow_run_id
            or observation.artifact_id != artifact.artifact_id
            or observation.artifact_name != expectation.artifact_name
            or observation.requirement_ids != expectation.requirement_ids
        ):
            diagnostics.append(
                _diagnostic(
                    code="stale_actions_resource",
                    stage="artifact_download",
                    head_sha=head_sha,
                    workflow_run_id=artifact.workflow_run_id,
                    artifact_name=expectation.artifact_name,
                    message=(
                        "Ignored an artifact observation that did not match its "
                        "exact-head expectation binding."
                    ),
                )
            )
            observations.append(
                _unverified_artifact(
                    expectation,
                    workflow_run_id=artifact.workflow_run_id,
                    head_sha=head_sha,
                    artifact=artifact,
                    reason=(
                        "The downloaded artifact did not match its exact-head "
                        "runtime evidence expectation."
                    ),
                )
            )
            continue
        observations.append(observation)

    if expectations and not completed_runs and not observations:
        diagnostics.append(
            _diagnostic(
                code="workflow_runs_incomplete",
                stage="artifacts",
                head_sha=head_sha,
                message=(
                    "Exact-head workflow runs are not completed, so missing runtime "
                    "artifacts have not been classified as present or absent."
                ),
            )
        )

    return _result(
        head_sha,
        job_logs=job_logs,
        artifacts=observations,
        diagnostics=diagnostics,
    )
