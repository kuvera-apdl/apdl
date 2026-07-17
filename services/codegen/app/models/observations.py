"""Strict immutable GitHub PR, CI, and remediation observation contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class GitHubPRStatus(str, Enum):
    draft = "draft"
    open = "open"
    merged = "merged"
    closed = "closed"


_POSTGRES_INTEGER_MAX = 2_147_483_647


class ExternalCIStatus(str, Enum):
    pending = "pending"
    passed = "passed"
    failed = "failed"
    unverified_external_ci = "unverified_external_ci"


class CIRemediationStatus(str, Enum):
    idle = "idle"
    diagnosing = "diagnosing"
    repairing = "repairing"
    awaiting_ci = "awaiting_ci"
    resolved = "resolved"
    exhausted = "exhausted"


class CISignalKind(str, Enum):
    check_run = "check_run"
    commit_status = "commit_status"


class CISignalConclusion(str, Enum):
    pending = "pending"
    passed = "passed"
    failed = "failed"
    neutral = "neutral"
    skipped = "skipped"


class RequirementVerificationStatus(str, Enum):
    pending = "pending"
    passed = "passed"
    failed = "failed"
    unverified = "unverified"


class FailureClassification(str, Enum):
    actionable_code = "actionable_code"
    flaky = "flaky"
    infrastructure = "infrastructure"
    policy = "policy"
    unknown = "unknown"


class RemediationDisposition(str, Enum):
    diagnosing = "diagnosing"
    awaiting_ci = "awaiting_ci"
    repaired = "repaired"
    rerun_requested = "rerun_requested"
    exhausted = "exhausted"
    superseded = "superseded"
    not_actionable = "not_actionable"


class RemediationPromptEvidence(StrictModel):
    evidence_id: str = Field(pattern=r"^prompt:[0-9a-f]{24}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: str = Field(min_length=1)
    label: str = Field(min_length=1)
    excerpt: str = Field(min_length=1, max_length=2000)


class CheckAnnotation(StrictModel):
    path: str = Field(min_length=1)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    level: Literal["notice", "warning", "failure"]
    message: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_range(self) -> CheckAnnotation:
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("annotation end_line cannot precede start_line")
        return self


class CISignal(StrictModel):
    signal_id: str = Field(min_length=1)
    kind: CISignalKind
    name: str = Field(min_length=1)
    conclusion: CISignalConclusion
    github_url: str | None = None
    check_suite_id: int | None = Field(default=None, ge=1)
    check_run_id: int | None = Field(default=None, ge=1)
    summary: str | None = None
    annotations: list[CheckAnnotation] = Field(default_factory=list)

    @model_validator(mode="after")
    def kind_has_canonical_identity(self) -> CISignal:
        if self.kind is CISignalKind.check_run:
            if self.check_run_id is None:
                raise ValueError("check_run signals require check_run_id")
            if self.signal_id != f"check_run:{self.check_run_id}":
                raise ValueError("check_run signal_id must be derived from check_run_id")
        elif self.check_run_id is not None or self.check_suite_id is not None:
            raise ValueError("commit_status signals cannot carry check identifiers")
        return self


class RequirementCIResult(StrictModel):
    requirement_id: str = Field(pattern=r"^REQ-[0-9]{3}$")
    evidence_id: str = Field(pattern=r"^CI-REQ-[0-9]{3}-[0-9]{2}$")
    status: RequirementVerificationStatus
    matched_signal_ids: list[str] = Field(default_factory=list)
    explanation: str = Field(min_length=1)

    @model_validator(mode="after")
    def exact_mapping(self) -> RequirementCIResult:
        if not self.evidence_id.startswith(f"CI-{self.requirement_id}-"):
            raise ValueError("evidence_id must be namespaced by requirement_id")
        if self.status in {
            RequirementVerificationStatus.passed,
            RequirementVerificationStatus.failed,
        } and not self.matched_signal_ids:
            raise ValueError("passed or failed requirement results need a matched signal")
        if len(self.matched_signal_ids) != len(set(self.matched_signal_ids)):
            raise ValueError("matched signal IDs must be unique")
        return self


class CIVerificationObservation(StrictModel):
    schema_version: Literal["ci_verification_observation@1"] = (
        "ci_verification_observation@1"
    )
    observation_id: str = Field(min_length=1)
    changeset_id: str = Field(min_length=1)
    repository: str = Field(pattern=r"^[^/]+/[^/]+$")
    pr_number: int = Field(ge=1, le=_POSTGRES_INTEGER_MAX)
    head_sha: str = Field(min_length=1)
    status: ExternalCIStatus
    signals: list[CISignal] = Field(default_factory=list)
    requirement_results: list[RequirementCIResult] = Field(default_factory=list)
    observed_at: datetime
    failure_key: str | None = None
    failure_summary: str | None = None

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "observed_at")

    @model_validator(mode="after")
    def result_matches_signals(self) -> CIVerificationObservation:
        signal_ids = [signal.signal_id for signal in self.signals]
        if len(signal_ids) != len(set(signal_ids)):
            raise ValueError("CI signal IDs must be unique")
        known = set(signal_ids)
        by_id = {signal.signal_id: signal for signal in self.signals}
        for result in self.requirement_results:
            if not set(result.matched_signal_ids) <= known:
                raise ValueError("requirement result refers to an unknown CI signal")
            matched = [by_id[value] for value in result.matched_signal_ids]
            if result.status is RequirementVerificationStatus.passed:
                if not any(
                    signal.conclusion is CISignalConclusion.passed
                    for signal in matched
                ) or any(
                    signal.conclusion
                    not in {CISignalConclusion.passed, CISignalConclusion.skipped}
                    for signal in matched
                ):
                    raise ValueError(
                        "passed requirement CI needs a genuinely passed signal"
                    )
            if result.status is RequirementVerificationStatus.failed and not any(
                signal.conclusion is CISignalConclusion.failed for signal in matched
            ):
                raise ValueError("failed requirement CI needs a failed signal")

        if self.status is ExternalCIStatus.passed:
            if not self.signals:
                raise ValueError("CI cannot pass without an observed signal")
            if any(
                signal.conclusion
                not in {CISignalConclusion.passed, CISignalConclusion.skipped}
                for signal in self.signals
            ):
                raise ValueError("passed CI cannot contain pending, failed, or neutral signals")
            if not any(
                signal.conclusion is CISignalConclusion.passed
                for signal in self.signals
            ):
                raise ValueError("CI cannot pass with only skipped signals")
        if self.status is ExternalCIStatus.unverified_external_ci and self.signals:
            raise ValueError("externally unverified CI cannot contain observed signals")
        if self.status is ExternalCIStatus.failed:
            if not any(
                signal.conclusion is CISignalConclusion.failed for signal in self.signals
            ):
                raise ValueError("failed CI requires a failed signal")
            if not self.failure_key or not self.failure_summary:
                raise ValueError("failed CI requires a failure key and summary")
        elif self.failure_key is not None or self.failure_summary is not None:
            raise ValueError("failure evidence is only valid for failed CI")
        return self

    def remediation_claim_scopes(self) -> tuple[str, ...]:
        """Stable failure scopes used to deduplicate repairs for this exact head.

        Check runs are grouped by check suite when GitHub supplies one, so a
        repeated poll or a newly-created run in the same failed suite cannot
        launch another repair for the same head. Commit statuses and check runs
        without suite metadata fall back to their canonical signal identity.
        """
        if self.status is not ExternalCIStatus.failed:
            return ()
        scopes = {
            (
                f"check_suite:{signal.check_suite_id}"
                if signal.check_suite_id is not None
                else signal.signal_id
            )
            for signal in self.signals
            if signal.conclusion is CISignalConclusion.failed
        }
        return tuple(sorted(scopes))

    def evidence_hash(self) -> str:
        """Stable immutable payload hash used to deduplicate repeated polls."""
        payload = self.model_dump(
            mode="json", exclude={"observation_id", "observed_at"}
        )
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()


class PullRequestObservation(StrictModel):
    schema_version: Literal["pull_request_observation@1"] = (
        "pull_request_observation@1"
    )
    observation_id: str = Field(min_length=1)
    delivery_id: str | None = Field(default=None, min_length=1)
    changeset_id: str = Field(min_length=1)
    repository: str = Field(pattern=r"^[^/]+/[^/]+$")
    pr_number: int = Field(ge=1, le=_POSTGRES_INTEGER_MAX)
    head_sha: str = Field(min_length=1)
    status: GitHubPRStatus
    action: Literal[
        "opened",
        "ready_for_review",
        "converted_to_draft",
        "synchronize",
        "closed",
        "reopened",
        "polled",
    ]
    github_url: str = Field(min_length=1)
    merge_sha: str | None = None
    github_updated_at: datetime
    observed_at: datetime

    @field_validator("github_updated_at", "observed_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "pull-request timestamp")

    @model_validator(mode="after")
    def merge_sha_only_for_merge(self) -> PullRequestObservation:
        if self.status is GitHubPRStatus.merged and not self.merge_sha:
            raise ValueError("merged pull-request observations require merge_sha")
        if self.status is not GitHubPRStatus.merged and self.merge_sha is not None:
            raise ValueError("merge_sha is only valid for a merged pull request")
        allowed_by_action = {
            "opened": {GitHubPRStatus.draft, GitHubPRStatus.open},
            "ready_for_review": {GitHubPRStatus.open},
            "converted_to_draft": {GitHubPRStatus.draft},
            "synchronize": {GitHubPRStatus.draft, GitHubPRStatus.open},
            "closed": {GitHubPRStatus.closed, GitHubPRStatus.merged},
            "reopened": {GitHubPRStatus.draft, GitHubPRStatus.open},
            "polled": set(GitHubPRStatus),
        }
        if self.status not in allowed_by_action[self.action]:
            raise ValueError(
                f"pull-request action {self.action!r} is inconsistent with "
                f"status {self.status.value!r}"
            )
        return self


class CIRemediationAttempt(StrictModel):
    """One immutable event in a logical CI-remediation attempt.

    ``attempt_id`` groups the attempt, while ``event_sequence`` and the derived
    ``event_id`` identify an append-only state event. Later outcomes are new
    records; an ``awaiting_ci`` record is never updated in place to ``repaired``
    or ``superseded``.
    """

    schema_version: Literal["ci_remediation_attempt@1"] = (
        "ci_remediation_attempt@1"
    )
    attempt_id: str = Field(min_length=1)
    event_sequence: int = Field(ge=1)
    event_id: str = Field(min_length=1)
    changeset_id: str = Field(min_length=1)
    repository: str = Field(pattern=r"^[^/]+/[^/]+$")
    pr_number: int = Field(ge=1, le=_POSTGRES_INTEGER_MAX)
    failed_head_sha: str = Field(min_length=1)
    failure_observation_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    classification: FailureClassification
    confidence: float = Field(ge=0, le=1)
    runtime_evidence_observation_id: str | None = Field(
        default=None, pattern=r"^runtime_obs_[0-9a-f]{32}$"
    )
    runtime_evidence_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    prompt_evidence_ids: list[str] = Field(default_factory=list)
    prompt_evidence: list[RemediationPromptEvidence] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    resulting_commit_sha: str | None = None
    disposition: RemediationDisposition
    started_at: datetime
    recorded_at: datetime
    finished_at: datetime | None = None
    error: str | None = None

    @field_validator("started_at", "recorded_at")
    @classmethod
    def required_timestamps_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "remediation timestamp")

    @field_validator("finished_at")
    @classmethod
    def finished_at_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_aware(value, "finished_at")

    @model_validator(mode="after")
    def disposition_has_result(self) -> CIRemediationAttempt:
        if self.event_id != f"{self.attempt_id}:{self.event_sequence}":
            raise ValueError("event_id must be derived from attempt_id and event_sequence")
        if self.recorded_at < self.started_at:
            raise ValueError("recorded_at cannot precede started_at")
        final = self.disposition not in {
            RemediationDisposition.diagnosing,
            RemediationDisposition.awaiting_ci,
        }
        if final and self.finished_at is None:
            raise ValueError("final remediation dispositions require finished_at")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.finished_at is not None and self.finished_at > self.recorded_at:
            raise ValueError("recorded_at cannot precede finished_at")
        if self.disposition in {
            RemediationDisposition.repaired,
            RemediationDisposition.awaiting_ci,
        } and not self.resulting_commit_sha:
            raise ValueError("a pushed repair requires resulting_commit_sha")
        if self.disposition is RemediationDisposition.rerun_requested and (
            self.changed_files or self.resulting_commit_sha
        ):
            raise ValueError("a CI rerun must not claim code changes or a commit")
        if len(self.prompt_evidence_ids) != len(set(self.prompt_evidence_ids)):
            raise ValueError("prompt evidence IDs must be unique")
        if (self.runtime_evidence_observation_id is None) != (
            self.runtime_evidence_hash is None
        ):
            raise ValueError(
                "runtime evidence observation ID and hash must be recorded together"
            )
        embedded_ids = [item.evidence_id for item in self.prompt_evidence]
        if embedded_ids != self.prompt_evidence_ids:
            raise ValueError(
                "prompt_evidence_ids must exactly match embedded prompt evidence"
            )
        if len(self.changed_files) != len(set(self.changed_files)):
            raise ValueError("changed files must be unique")
        return self


class ChangesetObservationHistory(StrictModel):
    """Read-only immutable GitHub evidence exposed for one changeset."""

    schema_version: Literal["changeset_observation_history@1"] = (
        "changeset_observation_history@1"
    )
    pull_requests: list[PullRequestObservation] = Field(default_factory=list)
    ci_verifications: list[CIVerificationObservation] = Field(default_factory=list)
    remediation_attempts: list[CIRemediationAttempt] = Field(default_factory=list)
