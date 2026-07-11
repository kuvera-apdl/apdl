"""Build immutable runtime observations from exact-head GitHub collections."""

from __future__ import annotations

from datetime import datetime

from app.models.observations import CIVerificationObservation
from app.runtime.collector import RuntimeEvidenceCollection
from app.runtime.models import RuntimeAcceptancePlan, RuntimeEvidenceObservation
from app.runtime.planner import assess_runtime_evidence


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
        values.append(f"{identity}: {item.message}")
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
        collection.artifacts,
        key=lambda item: (
            item.workflow_run_id,
            item.artifact_id or 0,
            item.artifact_name,
        ),
    )
    job_logs = sorted(
        collection.job_logs,
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
