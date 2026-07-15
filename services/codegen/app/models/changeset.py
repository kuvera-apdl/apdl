"""Canonical changeset domain model and lifecycle state machine.

A *changeset* is one unit of autonomous code work: clone a connected repo, edit
it to satisfy a task, test it, push a branch, open a pull request, and — once CI
is green and policy permits — merge. Exactly one canonical ``status`` field
tracks its lifecycle (Strict Schema Rule: no aliases, no parallel state).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChangesetStatus(str, Enum):
    """The single source of truth for where a changeset is in its lifecycle."""

    queued = "queued"
    cloning = "cloning"
    editing = "editing"
    testing = "testing"
    tests_failed = "tests_failed"  # terminal: local tests never went green; no PR
    pushing = "pushing"
    pr_open = "pr_open"
    ci_running = "ci_running"
    ci_failed = "ci_failed"
    ci_passed = "ci_passed"
    waiting_approval = "waiting_approval"
    merged = "merged"  # terminal: change landed on the base branch
    abandoned = "abandoned"  # terminal: PR closed / branch dropped
    error = "error"  # terminal: unexpected failure


#: Allowed forward transitions. Anything not listed is rejected by
#: :func:`assert_transition`, so the lifecycle cannot skip or reverse stages.
ALLOWED_TRANSITIONS: dict[ChangesetStatus, frozenset[ChangesetStatus]] = {
    ChangesetStatus.queued: frozenset(
        {ChangesetStatus.cloning, ChangesetStatus.abandoned, ChangesetStatus.error}
    ),
    ChangesetStatus.cloning: frozenset({ChangesetStatus.editing, ChangesetStatus.error}),
    ChangesetStatus.editing: frozenset({ChangesetStatus.testing, ChangesetStatus.error}),
    ChangesetStatus.testing: frozenset(
        {ChangesetStatus.tests_failed, ChangesetStatus.pushing, ChangesetStatus.error}
    ),
    ChangesetStatus.pushing: frozenset({ChangesetStatus.pr_open, ChangesetStatus.error}),
    ChangesetStatus.pr_open: frozenset(
        {ChangesetStatus.ci_running, ChangesetStatus.abandoned, ChangesetStatus.error}
    ),
    ChangesetStatus.ci_running: frozenset(
        {ChangesetStatus.ci_passed, ChangesetStatus.ci_failed, ChangesetStatus.error}
    ),
    ChangesetStatus.ci_failed: frozenset(
        {ChangesetStatus.ci_running, ChangesetStatus.abandoned, ChangesetStatus.error}
    ),
    ChangesetStatus.ci_passed: frozenset(
        {
            ChangesetStatus.waiting_approval,
            ChangesetStatus.merged,
            ChangesetStatus.abandoned,
            ChangesetStatus.error,
        }
    ),
    ChangesetStatus.waiting_approval: frozenset(
        {ChangesetStatus.merged, ChangesetStatus.abandoned, ChangesetStatus.error}
    ),
    # Terminal states intentionally map to the empty set.
    ChangesetStatus.tests_failed: frozenset(),
    ChangesetStatus.merged: frozenset(),
    ChangesetStatus.abandoned: frozenset(),
    ChangesetStatus.error: frozenset(),
}

#: Statuses from which a changeset can never move again.
TERMINAL_STATUSES: frozenset[ChangesetStatus] = frozenset(
    status for status, nxt in ALLOWED_TRANSITIONS.items() if not nxt
)

#: Failed outcomes a run can be *retried* from — a retry re-enqueues the same
#: task on a fresh changeset (new branch + PR) because the lifecycle cannot move
#: a terminal row backwards. ``merged`` is excluded (roll a landed change back
#: with /revert, not /retry); in-flight statuses are excluded (still running).
#: ``ci_failed`` is included even though it is technically non-terminal: its PR
#: is red and stuck, and a clean re-attempt is the natural next step.
RETRYABLE_STATUSES: frozenset[ChangesetStatus] = frozenset(
    {
        ChangesetStatus.tests_failed,
        ChangesetStatus.ci_failed,
        ChangesetStatus.error,
        ChangesetStatus.abandoned,
    }
)

#: Statuses from which a CI poll/webhook sync can still advance a changeset (its
#: PR is open and CI may report or be re-run). The single source of truth shared
#: by the sync (``jobs.ci``) and the poller's "what to sweep" query (``store``).
CI_SYNCABLE_STATUSES: frozenset[ChangesetStatus] = frozenset(
    {ChangesetStatus.pr_open, ChangesetStatus.ci_running, ChangesetStatus.ci_failed}
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
    draft: bool = True


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
    pr_node_id: str | None = None
    ci_status: str | None = None
    #: Merge commit SHA recorded at merge time; the deterministic revert target.
    merge_sha: str | None = None
    diff_stat: dict[str, Any] = Field(default_factory=dict)
    #: Ordered transcript of the LLM prompts the run sent (brief compilation,
    #: each edit instruction, each diff review) — see
    #: :class:`app.editor.base.EditResult`. Empty for runs that predate prompt
    #: recording or that never reached the editing stage.
    prompts: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class MergeRequest(BaseModel):
    """Request body for ``POST /v1/changesets/{id}/merge``."""

    model_config = ConfigDict(extra="forbid")

    merge_method: Literal["squash", "merge", "rebase"] = "squash"
