"""Canonical changeset domain model and lifecycle state machine.

A *changeset* is one unit of autonomous code work: clone a connected repo, edit
it to satisfy a task, push a branch, and open a pull request. GitHub owns CI
verification and merge; APDL only observes those external outcomes.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.models import ContractBundle
from app.evaluations.publication import PublicationAuthorization
from app.inspection.models import DependencySlice, InspectionSnapshot
from app.models.observations import (
    CIRemediationStatus,
    ExternalCIStatus,
    GitHubPRStatus,
)
from app.requirements.models import RequirementLedger
from app.runtime.models import RuntimeAcceptancePlan, RuntimeEvidenceAssessment
from app.semantic_review.models import ReviewVerdict
from app.verification.models import VerificationCoverage, VerificationPlan


class ChangesetStatus(str, Enum):
    """The single source of truth for where a changeset is in its lifecycle."""

    queued = "queued"
    cloning = "cloning"
    editing = "editing"
    pushing = "pushing"
    pr_open = "pr_open"
    merged = "merged"  # terminal: change landed on the base branch
    abandoned = "abandoned"  # GitHub closed it; a GitHub reopen can restore pr_open
    error = "error"  # terminal: unexpected failure


#: Allowed forward transitions. Anything not listed is rejected by
#: :func:`assert_transition`, so the lifecycle cannot skip or reverse stages.
ALLOWED_TRANSITIONS: dict[ChangesetStatus, frozenset[ChangesetStatus]] = {
    ChangesetStatus.queued: frozenset(
        {ChangesetStatus.cloning, ChangesetStatus.abandoned, ChangesetStatus.error}
    ),
    ChangesetStatus.cloning: frozenset({ChangesetStatus.editing, ChangesetStatus.error}),
    ChangesetStatus.editing: frozenset({ChangesetStatus.pushing, ChangesetStatus.error}),
    ChangesetStatus.pushing: frozenset({ChangesetStatus.pr_open, ChangesetStatus.error}),
    ChangesetStatus.pr_open: frozenset(
        {
            ChangesetStatus.merged,
            ChangesetStatus.abandoned,
            ChangesetStatus.error,
        }
    ),
    ChangesetStatus.abandoned: frozenset({ChangesetStatus.pr_open}),
    ChangesetStatus.merged: frozenset(),
    ChangesetStatus.error: frozenset(),
}

#: Statuses from which a changeset can never move again.
TERMINAL_STATUSES: frozenset[ChangesetStatus] = frozenset(
    status for status, nxt in ALLOWED_TRANSITIONS.items() if not nxt
)

#: Only a pre-PR generation error may be retried as fresh work. Open/closed PRs
#: remain GitHub-owned; CI failures repair the same branch and GitHub reopens PRs.
RETRYABLE_STATUSES: frozenset[ChangesetStatus] = frozenset(
    {ChangesetStatus.error}
)

#: Statuses from which a CI poll/webhook sync can still advance a changeset (its
#: PR is open and CI may report or be re-run). The single source of truth shared
#: by the sync (``jobs.ci``) and the poller's "what to sweep" query (``store``).
CI_SYNCABLE_STATUSES: frozenset[ChangesetStatus] = frozenset(
    {ChangesetStatus.pr_open}
)


class InvalidTransition(ValueError):
    """Raised when a changeset is moved between non-adjacent lifecycle states."""


def can_transition(current: ChangesetStatus, target: ChangesetStatus) -> bool:
    """Return ``True`` if ``current → target`` is a permitted lifecycle move."""
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_transition(current: ChangesetStatus, target: ChangesetStatus) -> None:
    """Raise :class:`InvalidTransition` unless ``current → target`` is allowed."""
    if not can_transition(current, target):
        raise InvalidTransition(
            f"Illegal changeset transition: {current.value} → {target.value}"
        )


# --- API payloads (Strict Schema Rule: unknown fields are rejected) ---------


class TaskSpec(BaseModel):
    """The implementation brief handed to the sandboxed coding engine."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    spec: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)


class ChangesetCreate(BaseModel):
    """Request body for ``POST /v1/changesets``."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    task: TaskSpec
    run_id: str | None = None
    base_branch: str | None = None


class Changeset(BaseModel):
    """Canonical changeset record as returned by the API."""

    model_config = ConfigDict(extra="forbid")

    changeset_id: str
    project_id: str
    run_id: str | None = None
    task: TaskSpec
    status: ChangesetStatus
    base_branch: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    github_pr_status: GitHubPRStatus | None = None
    external_ci_status: ExternalCIStatus | None = None
    #: When APDL first started awaiting GitHub evidence for the current PR head.
    #: This is diagnostic timing only; it cannot turn missing CI into a pass or
    #: age an open PR out of synchronization.
    external_ci_awaiting_since: datetime | None = None
    ci_retry_count: int = Field(default=0, ge=0)
    ci_remediation_status: CIRemediationStatus = CIRemediationStatus.idle
    ci_failure_key: str | None = None
    ci_failure_summary: str | None = None
    #: Merge commit SHA recorded at merge time; the deterministic revert target.
    merge_sha: str | None = None
    diff_stat: dict[str, Any] = Field(default_factory=dict)
    #: Ordered transcript of the LLM prompts the run sent (brief compilation,
    #: each edit instruction, each diff review) — see
    #: :class:`app.editor.base.EditResult`. Empty for runs that predate prompt
    #: recording or that never reached the editing stage.
    prompts: list[dict[str, Any]] = Field(default_factory=list)
    contract_bundle: ContractBundle | None = None
    requirement_ledger: RequirementLedger | None = None
    inspection_snapshot: InspectionSnapshot | None = None
    dependency_slice: DependencySlice | None = None
    verification_plan: VerificationPlan | None = None
    verification_coverage: VerificationCoverage | None = None
    runtime_acceptance_plan: RuntimeAcceptancePlan | None = None
    runtime_evidence_assessment: RuntimeEvidenceAssessment | None = None
    review_verdict: ReviewVerdict | None = None
    publication_authorization: PublicationAuthorization | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
