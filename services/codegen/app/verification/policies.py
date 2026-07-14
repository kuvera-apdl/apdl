"""Reusable Phase-5 risk-based verification policy packs."""

from __future__ import annotations

import re

from app.profiling import RepoProfile
from app.profiling.models import CommandKind
from app.requirements import (
    ImplementationStatus,
    Requirement,
    RequirementLedger,
    RequirementRisk,
)
from app.verification.models import (
    PlanDisposition,
    PlanItemDisposition,
    TestCommand,
    VerificationCheck,
    VerificationPlan,
    VerificationPlanItem,
    VerificationPolicyPack,
    VerificationPolicyRule,
    VerificationSurface,
)


def _pack(
    surface: VerificationSurface,
    *rules: tuple[VerificationCheck, str],
) -> VerificationPolicyPack:
    return VerificationPolicyPack(
        surface=surface,
        rules=[
            VerificationPolicyRule(check=check, description=description)
            for check, description in rules
        ],
    )


POLICY_PACKS: dict[VerificationSurface, VerificationPolicyPack] = {
    VerificationSurface.general: _pack(
        VerificationSurface.general,
        (
            VerificationCheck.regression,
            "Exercise the changed observable behavior without weakening existing checks.",
        ),
    ),
    VerificationSurface.ui: _pack(
        VerificationSurface.ui,
        (VerificationCheck.render, "Render the changed UI in its reachable context."),
        (VerificationCheck.interaction, "Exercise the user interaction and resulting state."),
        (
            VerificationCheck.accessibility_smoke,
            "Exercise keyboard and basic accessibility behavior.",
        ),
        (
            VerificationCheck.responsive_browser,
            "Exercise representative responsive browser geometry.",
        ),
    ),
    VerificationSurface.api: _pack(
        VerificationSurface.api,
        (VerificationCheck.route_existence, "Resolve and call the real route or handler."),
        (
            VerificationCheck.strict_request_response_schema,
            "Accept the canonical request/response shape and reject ambiguous fields.",
        ),
        (VerificationCheck.error_cases, "Exercise relevant invalid and failure cases."),
    ),
    VerificationSurface.sdk: _pack(
        VerificationSurface.sdk,
        (
            VerificationCheck.exact_version_contract,
            "Compile against the exact installed SDK contract.",
        ),
        (VerificationCheck.lifecycle, "Exercise SDK creation and lifecycle behavior."),
        (VerificationCheck.readiness, "Exercise asynchronous readiness before use."),
        (VerificationCheck.cleanup, "Exercise listener, client, and resource cleanup."),
    ),
    VerificationSurface.analytics: _pack(
        VerificationSurface.analytics,
        (VerificationCheck.canonical_event, "Assert the canonical event name and shape."),
        (VerificationCheck.real_sink, "Assert delivery to the repository's real sink."),
        (
            VerificationCheck.identity_consistency,
            "Assert identity remains consistent through the tracked flow.",
        ),
        (
            VerificationCheck.exposure_and_metric,
            "Assert exposure and primary metric behavior for experiment paths.",
        ),
    ),
    VerificationSurface.database: _pack(
        VerificationSurface.database,
        (
            VerificationCheck.migration_execution,
            "Apply the migration against the repository's database engine.",
        ),
        (
            VerificationCheck.rollback_or_forward_compatibility,
            "Exercise rollback or documented forward-compatible migration behavior.",
        ),
        (
            VerificationCheck.real_database_integration,
            "Exercise the behavior with a real database service in GitHub CI.",
        ),
    ),
    VerificationSurface.security: _pack(
        VerificationSurface.security,
        (VerificationCheck.unauthorized_path, "Assert unauthorized access is rejected."),
        (VerificationCheck.authorized_path, "Assert authorized access still succeeds."),
        (
            VerificationCheck.secret_and_permission_checks,
            "Assert secrets remain protected and permissions stay least-privileged.",
        ),
    ),
    VerificationSurface.billing: _pack(
        VerificationSurface.billing,
        (
            VerificationCheck.decimal_and_rounding,
            "Assert decimal precision, currency handling, and rounding boundaries.",
        ),
        (VerificationCheck.idempotency, "Assert repeated billing operations are idempotent."),
        (VerificationCheck.retry_behavior, "Assert billing retry behavior cannot double-charge."),
    ),
    VerificationSurface.concurrency: _pack(
        VerificationSurface.concurrency,
        (VerificationCheck.race_behavior, "Exercise the relevant concurrent race."),
        (VerificationCheck.retry_behavior, "Exercise bounded retry behavior."),
        (VerificationCheck.uniqueness, "Assert uniqueness under concurrent attempts."),
        (
            VerificationCheck.transactionality,
            "Assert atomic state and rollback at transaction boundaries.",
        ),
    ),
}

_KEYWORDS: dict[VerificationSurface, tuple[str, ...]] = {
    VerificationSurface.ui: (
        "ui",
        "page",
        "component",
        "button",
        "screen",
        "layout",
        "browser",
        "dom",
        "css",
        "accessible",
        "accessibility",
        "render",
        "responsive",
        "modal",
        "form",
    ),
    VerificationSurface.api: (
        "api",
        "endpoint",
        "route",
        "request",
        "response",
        "http",
        "handler",
        "webhook",
    ),
    VerificationSurface.sdk: (
        "sdk",
        "client library",
        "package integration",
        "installed package",
    ),
    VerificationSurface.analytics: (
        "analytics",
        "tracking",
        "telemetry",
        "metric",
        "experiment",
        "exposure",
        "conversion event",
    ),
    VerificationSurface.database: (
        "database",
        "migration",
        "postgres",
        "mysql",
        "sqlite",
        "sql table",
        "database table",
        "database column",
    ),
    VerificationSurface.security: (
        "security",
        "authentication",
        "authorization",
        "unauthorized",
        "authorized",
        "permission",
        "credential",
        "secret",
        "access control",
    ),
    VerificationSurface.billing: (
        "billing",
        "payment",
        "currency",
        "decimal",
        "rounding",
        "invoice",
        "charge",
        "price",
        "money",
    ),
    VerificationSurface.concurrency: (
        "concurrency",
        "concurrent",
        "race",
        "transaction",
        "uniqueness",
        "lock",
        "idempotent",
        "idempotency",
        "retry",
    ),
}


def _contains_keyword(text: str, keyword: str) -> bool:
    if " " in keyword:
        return keyword in text
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


def classify_requirement_surfaces(
    requirement: Requirement, profile: RepoProfile
) -> list[VerificationSurface]:
    """Classify from requested behavior, never from an unsupported model guess."""
    # The profile is intentionally accepted at this boundary: future adapters
    # can add evidence-backed classification without changing the public API.
    del profile
    text = " ".join(
        (
            requirement.original_source_text,
            requirement.observable_behavior,
            requirement.implementable_scope,
        )
    ).lower()
    surfaces = [
        surface
        for surface in VerificationSurface
        if surface is not VerificationSurface.general
        and any(_contains_keyword(text, keyword) for keyword in _KEYWORDS[surface])
    ]
    return surfaces or [VerificationSurface.general]


_RISK_ORDER = {
    RequirementRisk.low: 0,
    RequirementRisk.medium: 1,
    RequirementRisk.high: 2,
}


def _max_risk(requirements: list[Requirement]) -> RequirementRisk:
    if not requirements:
        return RequirementRisk.low
    return max((item.risk for item in requirements), key=_RISK_ORDER.__getitem__)


def build_verification_plan(
    ledger: RequirementLedger, profile: RepoProfile
) -> VerificationPlan:
    """Derive a deterministic policy plan; GitHub remains the only verifier."""
    active = [
        requirement
        for requirement in ledger.requirements
        if requirement.implementation_status
        not in {ImplementationStatus.blocked, ImplementationStatus.descoped}
    ]
    commands = sorted(
        (
            TestCommand(
                command=command.command,
                cwd=command.cwd,
                source_path=command.source_path,
            )
            for command in profile.commands
            if command.kind is CommandKind.test
        ),
        key=lambda command: (command.cwd, command.command, command.source_path),
    )
    runner = bool(profile.test_facilities or commands)
    workflows = sorted(
        {
            workflow.path
            for workflow in profile.ci_workflows
            if workflow.provider == "github_actions"
        }
    )
    protected = sorted(set(workflows).intersection(profile.protected_paths))

    if not active:
        disposition = PlanDisposition.no_implementable_requirements
        reason = "Every requirement is explicitly blocked or descoped."
    elif not runner:
        disposition = PlanDisposition.unverified_external_ci
        reason = (
            "No repository test runner was detected; APDL cannot represent the "
            "planned behavior as verified."
        )
    elif not workflows:
        disposition = PlanDisposition.unverified_external_ci
        reason = (
            "No GitHub Actions workflow was detected to execute the repository "
            "test coverage."
        )
    else:
        disposition = PlanDisposition.github_ci_planned
        reason = (
            "Repository test coverage is planned for execution by GitHub CI; "
            "no result exists until GitHub reports it."
        )

    items: list[VerificationPlanItem] = []
    for requirement in active:
        evidence_ids = [
            evidence.evidence_id for evidence in requirement.expected_ci_evidence
        ]
        for surface in classify_requirement_surfaces(requirement, profile):
            for rule in POLICY_PACKS[surface].rules:
                items.append(
                    VerificationPlanItem(
                        plan_item_id=f"VP-{len(items) + 1:03d}",
                        requirement_id=requirement.requirement_id,
                        surface=surface,
                        policy_check=rule.check,
                        requirement_risk=requirement.risk,
                        expected_assertion=(
                            f"{rule.description} Requirement behavior: "
                            f"{requirement.observable_behavior}"
                        ),
                        expected_ci_evidence_ids=evidence_ids,
                        requires_changed_test_for_pr=requirement.risk
                        in {RequirementRisk.medium, RequirementRisk.high},
                        disposition=(
                            PlanItemDisposition.required_in_github_ci
                            if disposition is PlanDisposition.github_ci_planned
                            else PlanItemDisposition.unverified_external_ci
                        ),
                    )
                )

    return VerificationPlan(
        source_ledger_sha256=ledger.source_sha256,
        repo_profile_schema_version=profile.schema_version,
        risk=_max_risk(active),
        test_runner_configured=runner,
        test_commands=commands,
        github_workflow_paths=workflows,
        protected_workflow_paths=protected,
        disposition=disposition,
        disposition_reason=reason,
        items=items,
    )
