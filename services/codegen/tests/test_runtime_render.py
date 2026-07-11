from app.runtime.models import (
    RuntimeAcceptancePlan,
    RuntimeArtifactExpectation,
    RuntimeCheck,
    RuntimeCommand,
    RuntimeEvidenceKind,
    RuntimeSurface,
)
from app.runtime.render import render_runtime_acceptance_plan


def _plan() -> RuntimeAcceptancePlan:
    return RuntimeAcceptancePlan(
        source_ledger_sha256="a" * 64,
        repo_profile_sha256="b" * 64,
        verification_plan_sha256="c" * 64,
        checks=[
            RuntimeCheck(
                check_id="runtime_0123456789abcdef",
                surface=RuntimeSurface.browser,
                requirement_ids=["REQ-001"],
                command=RuntimeCommand(
                    command="npm run test:e2e",
                    cwd=".",
                    source_path="package.json",
                ),
                expected_artifacts=[
                    RuntimeArtifactExpectation(
                        artifact_name="apdl-browser-evidence",
                        evidence_kind=RuntimeEvidenceKind.browser_report,
                        paths=["playwright-report/**"],
                        requirement_ids=["REQ-001"],
                    )
                ],
            )
        ]
    )


def test_runtime_plan_render_preserves_github_authority_and_workflow_boundary():
    rendered = render_runtime_acceptance_plan(
        _plan(), workflow_changes_authorized=False
    )

    assert "GitHub Actions is the only runtime-verification authority" in rendered
    assert "REQ-001" in rendered
    assert "apdl-browser-evidence" in rendered
    assert "Do not edit `.github/workflows/**`" in rendered
    assert "passed" in rendered
