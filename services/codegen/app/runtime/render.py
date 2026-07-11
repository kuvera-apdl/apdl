"""Stable agent-facing rendering for GitHub-owned runtime acceptance work."""

from __future__ import annotations

from app.runtime.models import RuntimeAcceptancePlan


def render_runtime_acceptance_plan(
    plan: RuntimeAcceptancePlan,
    *,
    workflow_changes_authorized: bool,
) -> str:
    """Render evidence expectations without claiming APDL can verify them."""
    lines = [
        "# GitHub CI Runtime Acceptance Plan",
        "",
        "GitHub Actions is the only runtime-verification authority. Add or update ",
        "repository-native tests so the relevant user/API/runtime path is exercised ",
        "and produces the exact artifacts below. Never fake an artifact, suppress a ",
        "failure, or represent a missing artifact as passed.",
        "Every artifact must contain one complete `apdl-runtime-evidence.json` ",
        "using `runtime_evidence_manifest@1`. Its `head_sha` must equal the ",
        "`APDL_RUNTIME_HEAD_SHA` environment variable and its sorted `requirements` ",
        "must bind each passed `REQ-NNN` to a concrete assertion. Optional ",
        "`evidence_files` entries must contain the exact artifact path and SHA-256. ",
        "A blank, truncated, redacted, stale-head, or malformed manifest is ",
        "unverified even when other report files exist.",
        "",
        "Workflow changes are "
        + ("explicitly authorized." if workflow_changes_authorized else "not authorized."),
    ]
    if not workflow_changes_authorized:
        lines.append(
            "Do not edit `.github/workflows/**`; add tests/evidence producers to the "
            "existing repository harness and leave unavailable runtime evidence explicit."
        )
    for check in plan.checks:
        lines.extend(
            [
                "",
                f"## {check.check_id} ({check.surface.value})",
                f"Requirements: {', '.join(check.requirement_ids)}",
                f"Repository command: `{check.command.command}`",
                f"Working directory: `{check.command.cwd}`",
            ]
        )
        for artifact in check.expected_artifacts:
            lines.append(
                f"Artifact `{artifact.artifact_name}` ({artifact.evidence_kind.value}) "
                f"must contain: {', '.join(artifact.paths)}"
            )
    if plan.blockers:
        lines.extend(["", "## Explicit blockers"])
        for blocker in plan.blockers:
            lines.append(
                f"- {blocker.requirement_id} ({blocker.surface.value}): {blocker.reason}"
            )
    return "\n".join(lines).rstrip() + "\n"
