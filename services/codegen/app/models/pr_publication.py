"""Strict append-only contracts for branch and pull-request publication.

Publication crosses two independently durable systems: PostgreSQL and GitHub.
These events preserve enough immutable intent and accepted GitHub identity to
resume after a process crash without creating a duplicate pull request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
)

from app.models.observations import ExternalCIStatus, GitHubPRStatus


_SHA_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_REPOSITORY_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_APDL_BRANCH_PATTERN = r"^apdl/[a-z0-9][a-z0-9._/-]{0,199}$"
_BASE_BRANCH_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$"
_MAX_PATCH_BASE64_LENGTH = 24 * 1024 * 1024
_POSTGRES_INTEGER_MAX = 2_147_483_647


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("publication timestamps must include a timezone")
    return value


class PullRequestAcceptedReceipt(BaseModel):
    """Raw identity retained immediately after GitHub accepts a create/read."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["pull_request_accepted_receipt@1"] = (
        "pull_request_accepted_receipt@1"
    )
    source: Literal["create", "recovery"]
    repository: str = Field(pattern=_REPOSITORY_PATTERN)
    requested_head: str = Field(pattern=_APDL_BRANCH_PATTERN)
    requested_base: str = Field(pattern=_BASE_BRANCH_PATTERN)
    accepted_at: datetime
    status_code: int = Field(ge=200, le=299)
    pr_number: int | None = Field(default=None, gt=0, le=_POSTGRES_INTEGER_MAX)
    github_url: str | None = Field(default=None, min_length=1, max_length=2048)
    raw_response: JsonValue

    _accepted_at_is_aware = field_validator("accepted_at")(_require_aware)

    @field_validator("github_url")
    @classmethod
    def validate_github_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("https://", "http://")):
            raise ValueError("GitHub pull-request URL must be absolute")
        return value


class _PublicationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    event_id: str = Field(pattern=r"^cpub_[0-9a-f]{32}$")
    changeset_id: str = Field(min_length=1, max_length=128)
    recorded_at: datetime

    _recorded_at_is_aware = field_validator("recorded_at")(_require_aware)


class PublicationIntentRecorded(_PublicationEvent):
    """Complete immutable input needed to resume branch and PR publication."""

    schema_version: Literal["pull_request_publication_intent@1"] = (
        "pull_request_publication_intent@1"
    )
    event_type: Literal["intent_recorded"] = "intent_recorded"
    repository: str = Field(pattern=_REPOSITORY_PATTERN)
    repository_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    branch: str = Field(pattern=_APDL_BRANCH_PATTERN)
    base_branch: str = Field(pattern=_BASE_BRANCH_PATTERN)
    candidate_base_sha: str = Field(pattern=_SHA_PATTERN)
    candidate_head_sha: str = Field(pattern=_SHA_PATTERN)
    candidate_tree_sha: str = Field(pattern=_SHA_PATTERN)
    patch_base64: str = Field(min_length=1, max_length=_MAX_PATCH_BASE64_LENGTH)
    commit_title: str = Field(min_length=1, max_length=200)
    pull_request_title: str = Field(min_length=1, max_length=200)
    pull_request_body: str = Field(min_length=1)
    draft: bool
    external_ci_status: ExternalCIStatus
    diff_stat: dict[str, JsonValue]


class PublicationBranchPublished(_PublicationEvent):
    schema_version: Literal["pull_request_branch_published@1"] = (
        "pull_request_branch_published@1"
    )
    event_type: Literal["branch_published"] = "branch_published"
    intent_event_id: str = Field(pattern=r"^cpub_[0-9a-f]{32}$")
    branch: str = Field(pattern=_APDL_BRANCH_PATTERN)
    head_sha: str = Field(pattern=_SHA_PATTERN)
    tree_sha: str = Field(pattern=_SHA_PATTERN)


class PublicationCreateAccepted(_PublicationEvent):
    schema_version: Literal["pull_request_create_accepted@1"] = (
        "pull_request_create_accepted@1"
    )
    event_type: Literal["create_accepted"] = "create_accepted"
    intent_event_id: str = Field(pattern=r"^cpub_[0-9a-f]{32}$")
    receipt: PullRequestAcceptedReceipt


class PublicationIdentityValidated(_PublicationEvent):
    schema_version: Literal["pull_request_identity_validated@1"] = (
        "pull_request_identity_validated@1"
    )
    event_type: Literal["identity_validated"] = "identity_validated"
    intent_event_id: str = Field(pattern=r"^cpub_[0-9a-f]{32}$")
    repository: str = Field(pattern=_REPOSITORY_PATTERN)
    repository_id: int = Field(gt=0)
    branch: str = Field(pattern=_APDL_BRANCH_PATTERN)
    base_branch: str = Field(pattern=_BASE_BRANCH_PATTERN)
    pr_number: int = Field(gt=0, le=_POSTGRES_INTEGER_MAX)
    github_url: str = Field(min_length=1, max_length=2048)
    head_sha: str = Field(pattern=_SHA_PATTERN)
    status: GitHubPRStatus
    github_updated_at: datetime

    _github_updated_at_is_aware = field_validator("github_updated_at")(_require_aware)


class PublicationCleanupRequested(_PublicationEvent):
    schema_version: Literal["pull_request_cleanup_requested@1"] = (
        "pull_request_cleanup_requested@1"
    )
    event_type: Literal["cleanup_requested"] = "cleanup_requested"
    intent_event_id: str = Field(pattern=r"^cpub_[0-9a-f]{32}$")
    pr_number: int = Field(gt=0, le=_POSTGRES_INTEGER_MAX)
    github_url: str | None = Field(default=None, min_length=1, max_length=2048)
    expected_head_sha: str = Field(pattern=_SHA_PATTERN)
    next_action: Literal["terminal_error", "continue_recovered"]
    reason: str = Field(min_length=1, max_length=2000)


class PublicationCleanupConfirmed(_PublicationEvent):
    schema_version: Literal["pull_request_cleanup_confirmed@1"] = (
        "pull_request_cleanup_confirmed@1"
    )
    event_type: Literal["cleanup_confirmed"] = "cleanup_confirmed"
    intent_event_id: str = Field(pattern=r"^cpub_[0-9a-f]{32}$")
    cleanup_request_event_id: str = Field(pattern=r"^cpub_[0-9a-f]{32}$")
    pr_number: int = Field(gt=0, le=_POSTGRES_INTEGER_MAX)
    github_url: str | None = Field(default=None, min_length=1, max_length=2048)
    next_action: Literal["terminal_error", "continue_recovered"]
    reason: str = Field(min_length=1, max_length=2000)


class PublicationManualIntervention(_PublicationEvent):
    schema_version: Literal["pull_request_manual_intervention@1"] = (
        "pull_request_manual_intervention@1"
    )
    event_type: Literal["manual_intervention"] = "manual_intervention"
    intent_event_id: str = Field(pattern=r"^cpub_[0-9a-f]{32}$")
    pr_number: int | None = Field(default=None, gt=0, le=_POSTGRES_INTEGER_MAX)
    github_url: str | None = Field(default=None, min_length=1, max_length=2048)
    reason: str = Field(min_length=1, max_length=4000)


class PublicationRecoveryDeferred(_PublicationEvent):
    schema_version: Literal["pull_request_recovery_deferred@1"] = (
        "pull_request_recovery_deferred@1"
    )
    event_type: Literal["recovery_deferred"] = "recovery_deferred"
    intent_event_id: str = Field(pattern=r"^cpub_[0-9a-f]{32}$")
    reason: str = Field(min_length=1, max_length=4000)


PullRequestPublicationEvent = Annotated[
    PublicationIntentRecorded
    | PublicationBranchPublished
    | PublicationCreateAccepted
    | PublicationIdentityValidated
    | PublicationCleanupRequested
    | PublicationCleanupConfirmed
    | PublicationManualIntervention
    | PublicationRecoveryDeferred,
    Field(discriminator="event_type"),
]

PULL_REQUEST_PUBLICATION_EVENT_ADAPTER = TypeAdapter(PullRequestPublicationEvent)
