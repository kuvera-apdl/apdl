"""Construct strict exact-head CI observations from GitHub API payloads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.models.observations import (
    CheckAnnotation,
    CISignal,
    CISignalConclusion,
    CISignalKind,
    CIVerificationObservation,
    ExternalCIStatus,
    GitHubPRStatus,
    PullRequestObservation,
    RequirementCIResult,
    RequirementVerificationStatus,
)
from app.requirements.models import GitHubCheckExpectation, RequirementLedger

_FAILED_CHECK_CONCLUSIONS = frozenset(
    {
        "action_required",
        "cancelled",
        "failure",
        "stale",
        "startup_failure",
        "timed_out",
    }
)


class CIObservationBuildError(ValueError):
    """Raised when GitHub evidence is malformed, conflicting, or stale."""


class StaleCIHeadError(CIObservationBuildError):
    """Raised when any supplied signal belongs to a different commit head."""


def _github_datetime(value: Any, source: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CIObservationBuildError(f"{source} is missing updated_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CIObservationBuildError(f"{source} has invalid updated_at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CIObservationBuildError(f"{source} updated_at must include a timezone")
    return parsed


def build_pull_request_observation(
    *,
    changeset_id: str,
    repository: str,
    action: str,
    pull_request: Mapping[str, Any],
    observed_at: datetime,
    delivery_id: str | None = None,
) -> PullRequestObservation:
    """Build a strict PR observation from one live GitHub PR representation."""
    number = pull_request.get("number")
    if not isinstance(number, int) or number < 1:
        raise CIObservationBuildError("pull request is missing a valid number")
    head = pull_request.get("head")
    if not isinstance(head, Mapping) or not str(head.get("sha") or "").strip():
        raise CIObservationBuildError("pull request is missing its exact head SHA")
    head_sha = str(head["sha"])
    url = str(pull_request.get("html_url") or "").strip()
    if not url:
        raise CIObservationBuildError("pull request is missing its GitHub URL")
    merged = pull_request.get("merged") is True
    state = str(pull_request.get("state") or "").lower()
    if merged:
        status = GitHubPRStatus.merged
    elif state == "closed":
        status = GitHubPRStatus.closed
    elif pull_request.get("draft") is True:
        status = GitHubPRStatus.draft
    elif state == "open":
        status = GitHubPRStatus.open
    else:
        raise CIObservationBuildError("pull request has an unknown GitHub state")
    merge_sha = (
        str(pull_request.get("merge_commit_sha") or "").strip() or None
        if merged
        else None
    )
    github_updated_at = _github_datetime(pull_request.get("updated_at"), "pull request")
    identity = {
        "changeset_id": changeset_id,
        "repository": repository,
        "number": number,
        "head_sha": head_sha,
        "status": status.value,
        "action": action,
        "github_updated_at": github_updated_at.isoformat(),
        "delivery_id": delivery_id,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    return PullRequestObservation(
        observation_id=f"pr_observation:{digest}",
        delivery_id=delivery_id,
        changeset_id=changeset_id,
        repository=repository,
        pr_number=number,
        head_sha=head_sha,
        status=status,
        action=action,
        github_url=url,
        merge_sha=merge_sha,
        github_updated_at=github_updated_at,
        observed_at=observed_at,
    )


def _bounded(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    marker = "\n[…truncated…]"
    return value[: max(0, limit - len(marker))].rstrip() + marker


def _assert_exact_head(value: Any, expected: str, source: str) -> None:
    if value is None or value == "":
        raise StaleCIHeadError(f"{source} is missing its head SHA")
    if str(value) != expected:
        raise StaleCIHeadError(
            f"{source} belongs to head {value!s}, not requested head {expected}"
        )


def _signal_name(value: Any, source: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise CIObservationBuildError(f"{source} is missing its name")
    if len(name) > 500 or "\n" in name or "\r" in name:
        raise CIObservationBuildError(f"{source} has an invalid or oversized name")
    return name


def _commit_conclusion(state: Any) -> CISignalConclusion:
    normalized = str(state or "").lower()
    if normalized == "success":
        return CISignalConclusion.passed
    if normalized in {"failure", "error"}:
        return CISignalConclusion.failed
    if normalized in {"pending", "queued", "in_progress"}:
        return CISignalConclusion.pending
    return CISignalConclusion.neutral


def _check_conclusion(run: Mapping[str, Any]) -> CISignalConclusion:
    if str(run.get("status") or "").lower() != "completed":
        return CISignalConclusion.pending
    conclusion = str(run.get("conclusion") or "").lower()
    if conclusion == "success":
        return CISignalConclusion.passed
    if conclusion == "skipped":
        return CISignalConclusion.skipped
    if conclusion in _FAILED_CHECK_CONCLUSIONS:
        return CISignalConclusion.failed
    return CISignalConclusion.neutral


def _commit_signal(
    status: Mapping[str, Any], head_sha: str, summary_cap: int
) -> CISignal:
    _assert_exact_head(status.get("sha"), head_sha, "commit status")
    name = _signal_name(status.get("context"), "commit status context")
    digest = hashlib.sha256(name.encode()).hexdigest()[:24]
    description = str(status.get("description") or "").strip()
    return CISignal(
        signal_id=f"commit_status:{digest}",
        kind=CISignalKind.commit_status,
        name=name,
        conclusion=_commit_conclusion(status.get("state")),
        github_url=(str(status["target_url"]) if status.get("target_url") else None),
        summary=_bounded(description, summary_cap) if description else None,
    )


def _annotation(
    value: Mapping[str, Any], *, message_cap: int
) -> CheckAnnotation | None:
    path = str(value.get("path") or "").strip()
    message = str(value.get("message") or value.get("title") or "").strip()
    if not path or not message:
        return None
    path = path.replace("\r", " ").replace("\n", " ")[:1000]
    level = str(value.get("annotation_level") or value.get("level") or "notice")
    if level not in {"notice", "warning", "failure"}:
        level = "notice"
    start_line = value.get("start_line")
    end_line = value.get("end_line")
    if not isinstance(start_line, int) or start_line < 1:
        start_line = None
    if not isinstance(end_line, int) or end_line < 1:
        end_line = None
    if start_line is not None and end_line is not None and end_line < start_line:
        end_line = start_line
    return CheckAnnotation(
        path=path,
        start_line=start_line,
        end_line=end_line,
        level=level,
        message=_bounded(message, message_cap),
    )


def _check_summary(run: Mapping[str, Any], summary_cap: int) -> str | None:
    output = run.get("output")
    if not isinstance(output, Mapping):
        return None
    values = [
        str(output.get(key) or "").strip() for key in ("title", "summary", "text")
    ]
    summary = "\n".join(value for value in values if value)
    return _bounded(summary, summary_cap) if summary else None


def _check_signal(
    run: Mapping[str, Any],
    head_sha: str,
    *,
    summary_cap: int,
    annotation_message_cap: int,
    max_annotations: int,
) -> CISignal:
    _assert_exact_head(run.get("head_sha"), head_sha, "check run")
    suite = run.get("check_suite")
    if isinstance(suite, Mapping):
        _assert_exact_head(suite.get("head_sha"), head_sha, "check suite")
    run_id = run.get("id")
    if not isinstance(run_id, int) or run_id < 1:
        raise CIObservationBuildError("check run is missing a positive integer id")
    name = _signal_name(run.get("name"), f"check run {run_id}")
    suite_id = None
    if isinstance(suite, Mapping) and isinstance(suite.get("id"), int):
        suite_id = suite["id"]
    elif isinstance(run.get("check_suite_id"), int):
        suite_id = run["check_suite_id"]

    raw_annotations = run.get("_failure_annotations") or run.get("annotations") or []
    annotations = []
    if isinstance(raw_annotations, Sequence) and not isinstance(
        raw_annotations, (str, bytes)
    ):
        for item in raw_annotations:
            if isinstance(item, Mapping):
                parsed = _annotation(item, message_cap=annotation_message_cap)
                if parsed is not None:
                    annotations.append(parsed)
    annotations = sorted(
        annotations,
        key=lambda item: (
            item.path,
            item.start_line or 0,
            item.end_line or 0,
            item.level,
            item.message,
        ),
    )[:max_annotations]
    return CISignal(
        signal_id=f"check_run:{run_id}",
        kind=CISignalKind.check_run,
        name=name,
        conclusion=_check_conclusion(run),
        github_url=(str(run["details_url"]) if run.get("details_url") else None),
        check_suite_id=suite_id,
        check_run_id=run_id,
        summary=_check_summary(run, summary_cap),
        annotations=annotations,
    )


def _deduplicate_signals(signals: list[CISignal]) -> list[CISignal]:
    by_id: dict[str, CISignal] = {}
    for signal in signals:
        previous = by_id.get(signal.signal_id)
        if previous is not None and previous != signal:
            raise CIObservationBuildError(
                f"conflicting GitHub payloads share signal id {signal.signal_id}"
            )
        by_id[signal.signal_id] = signal
    return sorted(
        by_id.values(),
        key=lambda signal: (signal.kind.value, signal.name, signal.signal_id),
    )


def _aggregate(signals: list[CISignal]) -> ExternalCIStatus:
    if not signals:
        return ExternalCIStatus.unverified_external_ci
    conclusions = {signal.conclusion for signal in signals}
    if CISignalConclusion.failed in conclusions:
        return ExternalCIStatus.failed
    if CISignalConclusion.pending in conclusions:
        return ExternalCIStatus.pending
    if (
        CISignalConclusion.passed in conclusions
        and conclusions <= {CISignalConclusion.passed, CISignalConclusion.skipped}
    ):
        return ExternalCIStatus.passed
    # Neutral or skipped-only evidence is observed but inconclusive. It must
    # never become either a pass or a no-signal result, so it remains pending
    # human/GitHub evidence.
    return ExternalCIStatus.pending


def _matched_requirement_status(
    signals: list[CISignal], external_status: ExternalCIStatus
) -> RequirementVerificationStatus:
    conclusions = {signal.conclusion for signal in signals}
    if CISignalConclusion.failed in conclusions:
        return RequirementVerificationStatus.failed
    if (
        CISignalConclusion.passed in conclusions
        and conclusions <= {CISignalConclusion.passed, CISignalConclusion.skipped}
    ):
        return RequirementVerificationStatus.passed
    if conclusions == {CISignalConclusion.skipped}:
        return RequirementVerificationStatus.unverified
    if signals:
        return RequirementVerificationStatus.pending
    if external_status is ExternalCIStatus.pending:
        return RequirementVerificationStatus.pending
    return RequirementVerificationStatus.unverified


def _requirement_results(
    ledger: RequirementLedger | None,
    signals: list[CISignal],
    external_status: ExternalCIStatus,
) -> list[RequirementCIResult]:
    if ledger is None:
        return []
    results: list[RequirementCIResult] = []
    for requirement in ledger.requirements:
        for expected in requirement.expected_ci_evidence:
            matched: list[CISignal] = []
            if isinstance(expected, GitHubCheckExpectation):
                # Exact means exact: no case-folding, prefix matching, aliases,
                # command inference, or fuzzy context matching.
                matched = [
                    signal for signal in signals if signal.name == expected.check_name
                ]
            status = _matched_requirement_status(matched, external_status)
            if matched:
                explanation = (
                    f"Exact GitHub check {expected.check_name!r} was observed as "
                    f"{status.value} for this head."
                )
            elif isinstance(expected, GitHubCheckExpectation):
                explanation = (
                    f"Exact GitHub check {expected.check_name!r} was not observed "
                    "for this head; no other signal is substituted."
                )
            else:
                explanation = (
                    f"Expected {expected.kind} evidence has no exact GitHub check "
                    "name; it remains pending or unverified until explicit evidence "
                    "is attached."
                )
            results.append(
                RequirementCIResult(
                    requirement_id=requirement.requirement_id,
                    evidence_id=expected.evidence_id,
                    status=status,
                    matched_signal_ids=sorted({signal.signal_id for signal in matched}),
                    explanation=explanation,
                )
            )
    return results


def _failure_evidence(
    signals: list[CISignal], head_sha: str, summary_cap: int
) -> tuple[str, str]:
    failed = [
        signal for signal in signals if signal.conclusion is CISignalConclusion.failed
    ]
    identity = json.dumps(
        [
            {
                "check_run_id": signal.check_run_id,
                "check_suite_id": signal.check_suite_id,
                "signal_id": signal.signal_id,
            }
            for signal in failed
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(f"{head_sha}:{identity}".encode()).hexdigest()[:24]
    sections: list[str] = []
    for signal in failed:
        lines = [f"{signal.name} ({signal.signal_id}): failed"]
        if signal.summary:
            lines.append(signal.summary)
        if signal.github_url:
            lines.append(signal.github_url)
        for annotation in signal.annotations:
            location = annotation.path
            if annotation.start_line:
                location += f":{annotation.start_line}"
            lines.append(f"- {location} [{annotation.level}]: {annotation.message}")
        sections.append("\n".join(lines))
    return f"ci_failure:{head_sha}:{digest}", _bounded(
        "\n\n".join(sections), summary_cap
    )


def build_ci_verification_observation(
    *,
    changeset_id: str,
    repository: str,
    pr_number: int,
    head_sha: str,
    combined_status: Mapping[str, Any],
    check_runs: Sequence[Mapping[str, Any]],
    observed_at: datetime,
    ledger: RequirementLedger | None = None,
    max_signals: int = 1000,
    max_annotations_per_signal: int = 50,
    max_signal_summary_chars: int = 4000,
    max_annotation_message_chars: int = 1000,
    max_failure_summary_chars: int = 12_000,
) -> CIVerificationObservation:
    """Build one immutable observation for exactly ``head_sha``.

    Missing evidence stays externally unverified. The combined rollup's own
    ``state`` is deliberately ignored: individual observed statuses/check runs
    are the auditable source for aggregation and requirement mapping.
    """
    if not head_sha:
        raise ValueError("head_sha is required")
    if (
        min(
            max_signals,
            max_annotations_per_signal,
            max_signal_summary_chars,
            max_annotation_message_chars,
            max_failure_summary_chars,
        )
        <= 0
    ):
        raise ValueError("CI observation budgets must be positive")
    _assert_exact_head(combined_status.get("sha"), head_sha, "combined status")
    raw_statuses = combined_status.get("statuses") or []
    if not isinstance(raw_statuses, Sequence) or isinstance(raw_statuses, (str, bytes)):
        raise CIObservationBuildError("combined statuses must be a list")
    if len(raw_statuses) + len(check_runs) > max_signals:
        raise CIObservationBuildError("GitHub CI signal count exceeds safety limit")

    signals: list[CISignal] = []
    for status in raw_statuses:
        if not isinstance(status, Mapping):
            raise CIObservationBuildError("commit status entries must be objects")
        signals.append(_commit_signal(status, head_sha, max_signal_summary_chars))
    for run in check_runs:
        if not isinstance(run, Mapping):
            raise CIObservationBuildError("check run entries must be objects")
        signals.append(
            _check_signal(
                run,
                head_sha,
                summary_cap=max_signal_summary_chars,
                annotation_message_cap=max_annotation_message_chars,
                max_annotations=max_annotations_per_signal,
            )
        )
    signals = _deduplicate_signals(signals)
    external_status = _aggregate(signals)
    requirement_results = _requirement_results(ledger, signals, external_status)
    failure_key = failure_summary = None
    if external_status is ExternalCIStatus.failed:
        failure_key, failure_summary = _failure_evidence(
            signals, head_sha, max_failure_summary_chars
        )

    temporary = CIVerificationObservation(
        observation_id="pending-stable-id",
        changeset_id=changeset_id,
        repository=repository,
        pr_number=pr_number,
        head_sha=head_sha,
        status=external_status,
        signals=signals,
        requirement_results=requirement_results,
        observed_at=observed_at,
        failure_key=failure_key,
        failure_summary=failure_summary,
    )
    payload = temporary.model_dump()
    payload["observation_id"] = "ciobs_" + temporary.evidence_hash()[:32]
    return CIVerificationObservation.model_validate(payload)
