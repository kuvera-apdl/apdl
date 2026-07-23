"""Build immutable runtime observations from exact-head GitHub collections."""

from __future__ import annotations

from datetime import datetime

from app.models.observations import CIVerificationObservation
from app.runtime.collector import (
    RuntimeEvidenceCollection,
    redact_artifact_observation,
)
from app.runtime.models import (
    RuntimeAcceptancePlan,
    RuntimeEvidenceObservation,
    RuntimeJobLogEvidence,
)
from app.runtime.planner import assess_runtime_evidence
from app.safety.secrets import redact_secrets


def _utf8_prefix(value: str, max_bytes: int) -> str:
    data = value.encode("utf-8")
    if len(data) <= max_bytes:
        return value
    return data[:max_bytes].decode("utf-8", "ignore")


def _redact_job_log(log: RuntimeJobLogEvidence) -> RuntimeJobLogEvidence:
    excerpt, changed = redact_secrets(log.text_excerpt)
    excerpt = _utf8_prefix(excerpt, 8000)
    job_name = redact_secrets(log.job_name)[0][:300] or "job"
    github_url = redact_secrets(log.github_url)[0][:2000]
    return RuntimeJobLogEvidence.model_validate(
        {
            **log.model_dump(mode="python"),
            "job_name": job_name,
            "text_excerpt": excerpt,
            "excerpt_byte_count": len(excerpt.encode("utf-8")),
            "redacted": log.redacted or changed,
            "github_url": github_url,
        }
    )


def _diagnostic_text(collection: RuntimeEvidenceCollection) -> list[str]:
    values = []
    for item in collection.diagnostics:
        identity = ":".join(
            value
            for value in (
                item.stage,
                item.code,
                str(item.workflow_run_id) if item.workflow_run_id else "",
                str(item.job_id) if item.job_id else "",
                item.artifact_name or "",
            )
            if value
        )
        values.append(redact_secrets(f"{identity}: {item.message}")[0])
    return sorted(set(values))


def build_runtime_evidence_observation(
    *,
    changeset_id: str,
    repository: str,
    pr_number: int,
    head_sha: str,
    ci_observation: CIVerificationObservation,
    plan: RuntimeAcceptancePlan,
    collection: RuntimeEvidenceCollection,
    observed_at: datetime,
) -> RuntimeEvidenceObservation:
    """Bind collected evidence to one PR head without changing GitHub's verdict."""
    if (
        ci_observation.changeset_id != changeset_id
        or ci_observation.repository != repository
        or ci_observation.pr_number != pr_number
        or ci_observation.head_sha != head_sha
    ):
        raise ValueError("runtime evidence must bind to the exact CI observation")
    if collection.head_sha != head_sha:
        raise ValueError("runtime collection does not match the requested exact head")
    artifacts = sorted(
        (redact_artifact_observation(item) for item in collection.artifacts),
        key=lambda item: (
            item.workflow_run_id,
            item.artifact_id or 0,
            item.artifact_name,
        ),
    )
    job_logs = sorted(
        (_redact_job_log(item) for item in collection.job_logs),
        key=lambda item: (item.workflow_run_id, item.job_id),
    )
    assessment = assess_runtime_evidence(
        plan,
        artifacts,
        head_sha=head_sha,
        external_ci_status=ci_observation.status,
    )
    temporary = RuntimeEvidenceObservation(
        observation_id="runtime_obs_" + "0" * 32,
        changeset_id=changeset_id,
        repository=repository,
        pr_number=pr_number,
        head_sha=head_sha,
        ci_observation_id=ci_observation.observation_id,
        ci_evidence_hash=ci_observation.evidence_hash(),
        runtime_acceptance_plan_sha256=plan.evidence_hash(),
        observed_at=observed_at,
        artifacts=artifacts,
        job_logs=job_logs,
        assessment=assessment,
        collection_errors=_diagnostic_text(collection),
    )
    payload = temporary.model_dump()
    payload["observation_id"] = "runtime_obs_" + temporary.evidence_hash()[:32]
    return RuntimeEvidenceObservation.model_validate(payload)
