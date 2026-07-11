"""Focused tests for reusable risk-based verification policy planning."""

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
from app.requirements import RequirementRisk, compile_requirement_ledger
from app.verification import (
    POLICY_PACKS,
    PlanDisposition,
    VerificationCheck,
    VerificationPlan,
    VerificationSurface,
    build_verification_plan,
    render_verification_plan,
)


def _profile(*, runner: bool = True, workflow: bool = True) -> RepoProfile:
    commands = (
        [
            RepoCommand(
                kind=CommandKind.test,
                command="pytest -q",
                cwd=".",
                source_path="pyproject.toml",
            )
        ]
        if runner
        else []
    )
    facilities = (
        [
            ProfileTestFacility(
                name="pytest", package_path=".", source_path="pyproject.toml"
            )
        ]
        if runner
        else []
    )
    workflows = (
        [CIWorkflow(provider="github_actions", path=".github/workflows/ci.yml")]
        if workflow
        else []
    )
    return RepoProfile(
        commands=commands,
        test_facilities=facilities,
        ci_workflows=workflows,
        protected_paths=[item.path for item in workflows],
    )


def test_policy_packs_cover_every_required_surface_and_rule_family():
    required = {
        VerificationSurface.ui,
        VerificationSurface.api,
        VerificationSurface.sdk,
        VerificationSurface.analytics,
        VerificationSurface.database,
        VerificationSurface.security,
        VerificationSurface.billing,
        VerificationSurface.concurrency,
    }
    assert required.issubset(POLICY_PACKS)
    assert {rule.check for rule in POLICY_PACKS[VerificationSurface.ui].rules} == {
        VerificationCheck.render,
        VerificationCheck.interaction,
        VerificationCheck.accessibility_smoke,
        VerificationCheck.responsive_browser,
    }
    assert {
        rule.check for rule in POLICY_PACKS[VerificationSurface.security].rules
    } == {
        VerificationCheck.unauthorized_path,
        VerificationCheck.authorized_path,
        VerificationCheck.secret_and_permission_checks,
    }
    assert {
        rule.check for rule in POLICY_PACKS[VerificationSurface.concurrency].rules
    } == {
        VerificationCheck.race_behavior,
        VerificationCheck.retry_behavior,
        VerificationCheck.uniqueness,
        VerificationCheck.transactionality,
    }


def test_plan_is_deterministic_and_expands_all_detected_surface_packs():
    ledger = compile_requirement_ledger(
        title="Secure billing API",
        spec="Add an authorized billing API endpoint with idempotent payment retries.",
        risk=RequirementRisk.high,
        github_check_name="api / pytest",
    )
    profile = _profile()

    first = build_verification_plan(ledger, profile)
    second = build_verification_plan(ledger, profile)

    assert first == second
    assert first.disposition is PlanDisposition.github_ci_planned
    assert first.authority == "github_ci"
    assert first.apdl_local_execution_authoritative is False
    assert first.workflow_gate_policy == "preserve_or_strengthen"
    assert first.risk is RequirementRisk.high
    assert {item.surface for item in first.items} == {
        VerificationSurface.api,
        VerificationSurface.security,
        VerificationSurface.billing,
        VerificationSurface.concurrency,
    }
    assert [item.plan_item_id for item in first.items] == [
        f"VP-{index:03d}" for index in range(1, len(first.items) + 1)
    ]
    assert all(item.requires_changed_test_for_pr for item in first.items)
    assert all(item.expected_ci_evidence_ids == ["CI-REQ-001-01"] for item in first.items)


@pytest.mark.parametrize(
    ("runner", "workflow", "expected_reason"),
    [
        (False, True, "No repository test runner"),
        (True, False, "No GitHub Actions workflow"),
    ],
)
def test_missing_runner_or_github_workflow_is_explicitly_unverified(
    runner, workflow, expected_reason
):
    ledger = compile_requirement_ledger(
        title="UI",
        spec="Render an accessible responsive settings page.",
        risk="medium",
    )

    plan = build_verification_plan(
        ledger, _profile(runner=runner, workflow=workflow)
    )

    assert plan.disposition is PlanDisposition.unverified_external_ci
    assert expected_reason in plan.disposition_reason
    assert plan.items
    assert all(item.disposition.value == "unverified_external_ci" for item in plan.items)


def test_blocked_or_descoped_requirements_do_not_fabricate_coverage():
    ledger = compile_requirement_ledger(
        title="External dependency",
        spec="Blocked: provision the production database — requires operator access",
        risk="high",
    )

    plan = build_verification_plan(ledger, _profile())

    assert plan.disposition is PlanDisposition.no_implementable_requirements
    assert plan.items == []


def test_unknown_surface_uses_general_regression_pack():
    ledger = compile_requirement_ledger(
        title="Refactor",
        spec="Preserve the requested observable behavior while simplifying the module.",
    )

    plan = build_verification_plan(ledger, _profile())

    assert len(plan.items) == 1
    assert plan.items[0].surface is VerificationSurface.general
    assert plan.items[0].policy_check is VerificationCheck.regression


def test_plan_schema_rejects_unknown_fields_and_non_github_authority():
    plan = build_verification_plan(
        compile_requirement_ledger(title="T", spec="Add an API endpoint."),
        _profile(),
    )
    payload = plan.model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        VerificationPlan.model_validate(payload)

    payload.pop("unexpected")
    payload["authority"] = "apdl_local"
    with pytest.raises(ValidationError):
        VerificationPlan.model_validate(payload)


def test_render_is_deterministic_and_never_claims_local_or_external_success():
    plan = build_verification_plan(
        compile_requirement_ledger(
            title="UI", spec="Render an accessible settings page."
        ),
        _profile(),
    )

    rendered = render_verification_plan(plan)

    assert rendered == render_verification_plan(plan)
    assert "verification_plan@1" in rendered
    assert "preserved or strengthened" in rendered
    assert "APDL does not execute authoritative verification" in rendered
    assert "only an exact GitHub result" in rendered
