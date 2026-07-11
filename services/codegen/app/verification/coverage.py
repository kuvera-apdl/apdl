"""Diff-path coverage enforcement for a planned GitHub CI verification set."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import PurePosixPath

from app.verification.models import (
    CoverageDisposition,
    CoverageItemStatus,
    PlanDisposition,
    VerificationCoverage,
    VerificationCoverageItem,
    VerificationPlan,
)

_TEST_FILE = re.compile(
    r"(?:^test_.+|.+_test|.+\.(?:test|spec))\.(?:py|js|jsx|ts|tsx|go|rs|java|kt|cs)$",
    re.IGNORECASE,
)


def _normalized_paths(paths: Sequence[str]) -> list[str]:
    normalized: set[str] = set()
    for raw in paths:
        value = raw.strip().replace("\\", "/")
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Changed path must be repository-relative: {raw!r}")
        normalized.add(path.as_posix())
    return sorted(normalized)


def is_test_path(path: str) -> bool:
    normalized = PurePosixPath(path.replace("\\", "/"))
    parts = {part.lower() for part in normalized.parts[:-1]}
    if parts.intersection({"test", "tests", "__tests__"}):
        return True
    if "src" in parts and "test" in parts:
        return True
    return _TEST_FILE.fullmatch(normalized.name) is not None


def is_github_workflow_path(path: str) -> bool:
    normalized = PurePosixPath(path.replace("\\", "/"))
    return (
        len(normalized.parts) >= 3
        and normalized.parts[:2] == (".github", "workflows")
        and normalized.suffix.lower() in {".yml", ".yaml"}
    )


def evaluate_verification_coverage(
    plan: VerificationPlan,
    *,
    changed_paths: Sequence[str],
    relaxed_workflow_paths: Sequence[str] = (),
) -> VerificationCoverage:
    """Enforce coverage presence without claiming that any test has passed."""
    changed = _normalized_paths(changed_paths)
    relaxed = _normalized_paths(relaxed_workflow_paths)
    tests = sorted(path for path in changed if is_test_path(path))
    workflows = sorted(path for path in changed if is_github_workflow_path(path))
    changed_protected = sorted(
        set(workflows).intersection(plan.protected_workflow_paths)
    )

    if relaxed:
        disposition = CoverageDisposition.rejected_workflow_gate_relaxation
        reason = (
            "Protected GitHub workflow gates may only be preserved or strengthened; "
            "the proposed relaxation is rejected."
        )
    elif changed_protected:
        disposition = CoverageDisposition.requires_protected_workflow_review
        reason = (
            "An existing protected GitHub workflow changed and requires evidence "
            "that its gates were preserved or strengthened."
        )
    elif plan.disposition is PlanDisposition.no_implementable_requirements:
        disposition = CoverageDisposition.no_implementable_requirements
        reason = "There are no implementable requirements to cover."
    elif not plan.test_runner_configured:
        disposition = CoverageDisposition.unverified_external_ci
        reason = (
            "No repository test runner was detected; changed test or workflow "
            "paths alone cannot be represented as verified."
        )
    elif not (plan.github_workflow_paths or workflows):
        disposition = CoverageDisposition.unverified_external_ci
        reason = "No existing or newly added GitHub workflow can execute the coverage."
    elif any(item.requires_changed_test_for_pr for item in plan.items) and not tests:
        disposition = CoverageDisposition.missing_required_coverage
        reason = "Medium/high-risk requirements need a changed test path before PR creation."
    else:
        disposition = CoverageDisposition.ready_for_github_ci
        reason = (
            "Required coverage is present for GitHub CI to execute; this is not a "
            "verification result."
        )

    if disposition is CoverageDisposition.rejected_workflow_gate_relaxation:
        item_status = CoverageItemStatus.rejected_workflow_gate_relaxation
    elif disposition is CoverageDisposition.requires_protected_workflow_review:
        item_status = CoverageItemStatus.requires_protected_workflow_review
    elif disposition is CoverageDisposition.unverified_external_ci:
        item_status = CoverageItemStatus.unverified_external_ci
    elif disposition is CoverageDisposition.missing_required_coverage:
        item_status = CoverageItemStatus.missing_required_coverage
    elif tests:
        item_status = CoverageItemStatus.coverage_path_present
    else:
        item_status = CoverageItemStatus.planned_in_github_ci

    items = [
        VerificationCoverageItem(
            plan_item_id=item.plan_item_id,
            status=item_status,
            coverage_paths=tests,
        )
        for item in plan.items
    ]
    return VerificationCoverage(
        source_ledger_sha256=plan.source_ledger_sha256,
        disposition=disposition,
        disposition_reason=reason,
        changed_test_paths=tests,
        changed_workflow_paths=workflows,
        changed_protected_workflow_paths=changed_protected,
        relaxed_workflow_paths=relaxed,
        items=items,
    )
