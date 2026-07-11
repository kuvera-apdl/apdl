"""Focused tests for changed-test/workflow coverage enforcement."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.profiling import RepoProfile
from app.profiling.models import (
    CIWorkflow,
    CommandKind,
    RepoCommand,
    TestFacility as ProfileTestFacility,
)
from app.requirements import compile_requirement_ledger
from app.verification import (
    CoverageDisposition,
    CoverageItemStatus,
    VerificationCoverage,
    build_verification_plan,
    evaluate_verification_coverage,
    is_github_workflow_path,
    is_test_path,
    render_verification_coverage,
)


def _plan(
    *,
    risk: str = "medium",
    runner: bool = True,
    workflow: bool = True,
    protected: bool = True,
):
    workflow_path = ".github/workflows/ci.yml"
    profile = RepoProfile(
        commands=(
            [
                RepoCommand(
                    kind=CommandKind.test,
                    command="npm test",
                    cwd=".",
                    source_path="package.json",
                )
            ]
            if runner
            else []
        ),
        test_facilities=(
            [
                ProfileTestFacility(
                    name="vitest", package_path=".", source_path="package.json"
                )
            ]
            if runner
            else []
        ),
        ci_workflows=(
            [CIWorkflow(provider="github_actions", path=workflow_path)]
            if workflow
            else []
        ),
        protected_paths=[workflow_path] if workflow and protected else [],
    )
    ledger = compile_requirement_ledger(
        title="Settings UI",
        spec="Render an accessible settings page and handle button interaction.",
        risk=risk,
    )
    return build_verification_plan(ledger, profile)


def test_medium_and_high_risk_require_a_changed_test_path():
    coverage = evaluate_verification_coverage(
        _plan(risk="high"), changed_paths=["src/Settings.tsx"]
    )

    assert coverage.disposition is CoverageDisposition.missing_required_coverage
    assert coverage.changed_test_paths == []
    assert coverage.github_has_not_reported is True
    assert coverage.apdl_declared_verified is False
    assert all(
        item.status is CoverageItemStatus.missing_required_coverage
        for item in coverage.items
    )


def test_changed_test_makes_coverage_ready_for_github_not_verified():
    coverage = evaluate_verification_coverage(
        _plan(risk="medium"),
        changed_paths=["src/Settings.tsx", "src/__tests__/Settings.test.tsx"],
    )

    assert coverage.disposition is CoverageDisposition.ready_for_github_ci
    assert coverage.changed_test_paths == ["src/__tests__/Settings.test.tsx"]
    assert all(
        item.status is CoverageItemStatus.coverage_path_present
        for item in coverage.items
    )
    assert "not a verification result" in coverage.disposition_reason


def test_low_risk_without_changed_test_can_be_planned_but_not_passed():
    coverage = evaluate_verification_coverage(
        _plan(risk="low"), changed_paths=["src/Settings.tsx"]
    )

    assert coverage.disposition is CoverageDisposition.ready_for_github_ci
    assert all(
        item.status is CoverageItemStatus.planned_in_github_ci
        for item in coverage.items
    )
    assert coverage.github_has_not_reported is True


def test_no_runner_remains_unverified_even_when_test_and_workflow_paths_change():
    coverage = evaluate_verification_coverage(
        _plan(runner=False, workflow=False),
        changed_paths=[
            "tests/settings.test.ts",
            ".github/workflows/ci.yml",
        ],
    )

    assert coverage.disposition is CoverageDisposition.unverified_external_ci
    assert "No repository test runner" in coverage.disposition_reason
    assert all(
        item.status is CoverageItemStatus.unverified_external_ci
        for item in coverage.items
    )


def test_new_workflow_plus_test_can_cover_repo_with_runner_but_no_old_workflow():
    coverage = evaluate_verification_coverage(
        _plan(runner=True, workflow=False),
        changed_paths=[
            "tests/settings.test.ts",
            ".github/workflows/ci.yml",
        ],
    )

    assert coverage.disposition is CoverageDisposition.ready_for_github_ci
    assert coverage.changed_workflow_paths == [".github/workflows/ci.yml"]


def test_existing_protected_workflow_change_requires_review_not_auto_acceptance():
    coverage = evaluate_verification_coverage(
        _plan(protected=True),
        changed_paths=[
            "tests/settings.test.ts",
            ".github/workflows/ci.yml",
        ],
    )

    assert (
        coverage.disposition
        is CoverageDisposition.requires_protected_workflow_review
    )
    assert coverage.changed_protected_workflow_paths == [
        ".github/workflows/ci.yml"
    ]
    assert all(
        item.status is CoverageItemStatus.requires_protected_workflow_review
        for item in coverage.items
    )


def test_declared_workflow_gate_relaxation_is_always_rejected():
    coverage = evaluate_verification_coverage(
        _plan(),
        changed_paths=[".github/workflows/ci.yml"],
        relaxed_workflow_paths=[".github/workflows/ci.yml"],
    )

    assert (
        coverage.disposition
        is CoverageDisposition.rejected_workflow_gate_relaxation
    )
    assert coverage.workflow_gate_policy == "preserve_or_strengthen"
    assert all(
        item.status is CoverageItemStatus.rejected_workflow_gate_relaxation
        for item in coverage.items
    )


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_api.py",
        "src/__tests__/view.tsx",
        "src/view.test.tsx",
        "pkg/handler_test.go",
        "src/test/java/AppTest.java",
    ],
)
def test_test_path_detection_is_ecosystem_general(path):
    assert is_test_path(path)


def test_workflow_path_detection_is_exact_and_rejects_lookalikes():
    assert is_github_workflow_path(".github/workflows/ci.yml")
    assert is_github_workflow_path(".github/workflows/runtime.yaml")
    assert not is_github_workflow_path("docs/.github/workflows/ci.yml")
    assert not is_github_workflow_path(".github/workflows/README.md")


def test_coverage_schema_is_strict_and_render_repeats_authority_boundary():
    coverage = evaluate_verification_coverage(
        _plan(), changed_paths=["tests/settings.test.ts"]
    )
    payload = coverage.model_dump(mode="json")
    payload["passed"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        VerificationCoverage.model_validate(payload)

    rendered = render_verification_coverage(coverage)
    assert rendered == render_verification_coverage(coverage)
    assert "verification_coverage@1" in rendered
    assert "not a test result" in rendered
    assert "APDL has not declared the change verified" in rendered


def test_changed_paths_must_be_repository_relative():
    with pytest.raises(ValueError, match="repository-relative"):
        evaluate_verification_coverage(
            _plan(), changed_paths=["../outside/tests/test_api.py"]
        )
