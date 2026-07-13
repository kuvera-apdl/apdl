"""Runtime planning, workflow rendering, and evidence-assessment tests."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

import pytest
from pydantic import ValidationError

from app.profiling.models import (
    CodeSurface,
    CommandKind,
    DeploymentTarget,
    PackageManager,
    RepoCommand,
    RepoProfile,
    TestFacility as ProfileTestFacility,
)
from app.models.observations import ExternalCIStatus
from app.requirements import RequirementRisk
from app.runtime.github_actions import (
    WorkflowGenerationNotAuthorized,
    render_github_actions_workflow,
)
from app.runtime.models import (
    ArtifactFileEvidence,
    RUNTIME_ACCEPTANCE_WORKFLOW_PATH,
    RuntimeAcceptancePlan,
    RuntimeAcceptancePolicy,
    RuntimeAcceptanceRequest,
    RuntimeArtifactExpectation,
    RuntimeArtifactObservation,
    RuntimeCommand,
    RuntimeEvidenceKind,
    RuntimeEvidenceObservation,
    RuntimeEvidenceStatus,
    RuntimeJobLogEvidence,
    RuntimeRequirement,
    RuntimeSurface,
)
from app.runtime.planner import (
    assess_runtime_evidence,
    build_runtime_acceptance_plan,
    derive_runtime_requirements,
    plan_runtime_acceptance,
)
from app.verification.models import (
    PlanDisposition,
    PlanItemDisposition,
    TestCommand as VerificationTestCommand,
    VerificationCheck,
    VerificationPlan,
    VerificationPlanItem,
    VerificationSurface,
)


def _profile(
    *,
    with_command: bool = True,
    command: str = "npm run test:runtime",
    cwd: str = ".",
) -> RepoProfile:
    commands = (
        [
            RepoCommand(
                kind=CommandKind.test,
                command=command,
                cwd=cwd,
                source_path="package.json",
            )
        ]
        if with_command
        else []
    )
    return RepoProfile(
        repo="acme/widgets",
        branch="main",
        commands=commands,
        package_managers=(
            [
                PackageManager(
                    name="npm",
                    manifest_path="package.json",
                    lockfile_path="package-lock.json",
                )
            ]
            if with_command
            else []
        ),
        test_facilities=[
            ProfileTestFacility(
                name="playwright",
                package_path=cwd,
                browser=True,
                source_path="package.json",
            )
        ],
        routes=[
            CodeSurface(
                kind="api_route",
                path="app/api/health/route.ts",
                package_path=cwd,
            )
        ],
        entrypoints=[CodeSurface(kind="app", path="app/page.tsx", package_path=cwd)],
        deployment_targets=[
            DeploymentTarget(kind="docker_compose", path="compose.yaml")
        ],
    )


def _requirements() -> list[RuntimeRequirement]:
    return [
        RuntimeRequirement(requirement_id="REQ-001", surface=RuntimeSurface.browser),
        RuntimeRequirement(requirement_id="REQ-002", surface=RuntimeSurface.api),
        RuntimeRequirement(
            requirement_id="REQ-003", surface=RuntimeSurface.service_container
        ),
        RuntimeRequirement(requirement_id="REQ-004", surface=RuntimeSurface.runtime),
    ]


def _verification_plan() -> VerificationPlan:
    surfaces = [
        VerificationSurface.ui,
        VerificationSurface.api,
        VerificationSurface.database,
        VerificationSurface.sdk,
        VerificationSurface.analytics,
        VerificationSurface.security,
        VerificationSurface.billing,
        VerificationSurface.concurrency,
        VerificationSurface.general,
    ]
    checks = {
        VerificationSurface.ui: VerificationCheck.render,
        VerificationSurface.api: VerificationCheck.route_existence,
        VerificationSurface.database: VerificationCheck.migration_execution,
        VerificationSurface.sdk: VerificationCheck.lifecycle,
        VerificationSurface.analytics: VerificationCheck.canonical_event,
        VerificationSurface.security: VerificationCheck.unauthorized_path,
        VerificationSurface.billing: VerificationCheck.idempotency,
        VerificationSurface.concurrency: VerificationCheck.race_behavior,
        VerificationSurface.general: VerificationCheck.regression,
    }
    items = [
        VerificationPlanItem(
            plan_item_id=f"VP-{index:03d}",
            requirement_id=f"REQ-{index:03d}",
            surface=surface,
            policy_check=checks[surface],
            requirement_risk=RequirementRisk.medium,
            expected_assertion=f"Exercise {surface.value} behavior.",
            expected_ci_evidence_ids=[f"CI-REQ-{index:03d}-01"],
            requires_changed_test_for_pr=True,
            disposition=PlanItemDisposition.required_in_github_ci,
        )
        for index, surface in enumerate(surfaces, start=1)
    ]
    # A second policy check for the UI requirement must collapse to the same
    # runtime requirement rather than creating a duplicate browser job mapping.
    items.append(
        VerificationPlanItem(
            plan_item_id="VP-010",
            requirement_id="REQ-001",
            surface=VerificationSurface.ui,
            policy_check=VerificationCheck.interaction,
            requirement_risk=RequirementRisk.medium,
            expected_assertion="Exercise UI interaction.",
            expected_ci_evidence_ids=["CI-REQ-001-01"],
            requires_changed_test_for_pr=True,
            disposition=PlanItemDisposition.required_in_github_ci,
        )
    )
    return VerificationPlan(
        source_ledger_sha256="a" * 64,
        repo_profile_schema_version="repo_profile@1",
        risk=RequirementRisk.medium,
        test_runner_configured=True,
        test_commands=[
            VerificationTestCommand(
                command="npm run test:runtime",
                cwd=".",
                source_path="package.json",
            )
        ],
        github_workflow_paths=[".github/workflows/ci.yml"],
        protected_workflow_paths=[],
        disposition=PlanDisposition.github_ci_planned,
        disposition_reason="GitHub CI is configured.",
        items=items,
    )


def _artifact_file(path: str = "apdl-runtime-evidence.json") -> ArtifactFileEvidence:
    return ArtifactFileEvidence(
        path=path,
        content_sha256="a" * 64,
        byte_count=2,
        text_excerpt="{}",
    )


def _manifest_file(
    *, head_sha: str = "head-new", requirement_ids: list[str] | None = None
) -> ArtifactFileEvidence:
    payload = json.dumps(
        {
            "schema_version": "runtime_evidence_manifest@1",
            "head_sha": head_sha,
            "requirements": [
                {
                    "requirement_id": requirement_id,
                    "status": "passed",
                    "assertion": "The runtime behavior matched the requirement.",
                    "evidence_files": [],
                }
                for requirement_id in (requirement_ids or ["REQ-001"])
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return ArtifactFileEvidence(
        path="apdl-runtime-evidence.json",
        content_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        byte_count=len(payload.encode()),
        text_excerpt=payload,
    )


def _runtime_plan(
    profile: RepoProfile,
    requirements: list[RuntimeRequirement],
) -> RuntimeAcceptancePlan:
    return plan_runtime_acceptance(
        profile,
        requirements,
        source_ledger_sha256="a" * 64,
        verification_plan_sha256="b" * 64,
    )


def _model_hash(value: RepoProfile | VerificationPlan) -> str:
    payload = json.dumps(
        value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def test_verification_surfaces_derive_deterministic_runtime_requirements():
    plan = _verification_plan()

    requirements = derive_runtime_requirements(plan)

    assert requirements == derive_runtime_requirements(plan)
    assert [(item.requirement_id, item.surface) for item in requirements] == [
        ("REQ-001", RuntimeSurface.browser),
        ("REQ-002", RuntimeSurface.api),
        ("REQ-003", RuntimeSurface.service_container),
        ("REQ-004", RuntimeSurface.runtime),
        ("REQ-005", RuntimeSurface.runtime),
        ("REQ-006", RuntimeSurface.runtime),
        ("REQ-007", RuntimeSurface.runtime),
        ("REQ-008", RuntimeSurface.runtime),
        ("REQ-009", RuntimeSurface.runtime),
    ]


def test_canonical_runtime_builder_binds_profile_and_verification_provenance():
    profile = _profile()
    verification_plan = _verification_plan()

    first = build_runtime_acceptance_plan(profile, verification_plan)
    second = build_runtime_acceptance_plan(profile, verification_plan)

    assert first == second
    assert first.source_ledger_sha256 == verification_plan.source_ledger_sha256
    assert first.repo_profile_sha256 == _model_hash(profile)
    assert first.verification_plan_sha256 == _model_hash(verification_plan)
    assert {
        (requirement_id, check.surface)
        for check in first.checks
        for requirement_id in check.requirement_ids
    } == {
        ("REQ-002", RuntimeSurface.api),
        ("REQ-001", RuntimeSurface.browser),
        ("REQ-003", RuntimeSurface.service_container),
        ("REQ-004", RuntimeSurface.runtime),
        ("REQ-005", RuntimeSurface.runtime),
        ("REQ-006", RuntimeSurface.runtime),
        ("REQ-007", RuntimeSurface.runtime),
        ("REQ-008", RuntimeSurface.runtime),
        ("REQ-009", RuntimeSurface.runtime),
    }


def test_runtime_plan_uses_only_exact_profile_commands_and_surfaces():
    profile = _profile()
    plan = _runtime_plan(profile, _requirements())

    assert plan.schema_version == "runtime_acceptance_plan@1"
    assert len(plan.checks) == 4
    assert plan.blockers == []
    assert {check.command.command for check in plan.checks} == {"npm run test:runtime"}
    assert all(check.command.source_path == "package.json" for check in plan.checks)
    service = next(
        check
        for check in plan.checks
        if check.surface is RuntimeSurface.service_container
    )
    assert service.service_container_paths == ["compose.yaml"]
    assert plan == _runtime_plan(profile, _requirements())

    with pytest.raises(ValidationError):
        RuntimeAcceptancePlan.model_validate(
            {**plan.model_dump(mode="json"), "unknown": True}
        )
    with pytest.raises(ValueError, match="must be unique"):
        _runtime_plan(profile, [*_requirements(), _requirements()[0]])


def test_unknown_runtime_commands_create_blockers_instead_of_fallbacks():
    plan = _runtime_plan(_profile(with_command=False), _requirements())

    assert plan.checks == []
    assert {blocker.requirement_id for blocker in plan.blockers} == {
        requirement.requirement_id for requirement in _requirements()
    }
    assert all(
        "no repository-declared test command" in item.reason for item in plan.blockers
    )


def test_generic_runtime_surface_uses_declared_command_and_locked_setup():
    profile = RepoProfile(
        repo="acme/library",
        branch="main",
        commands=[
            RepoCommand(
                kind=CommandKind.test,
                command="pytest -q",
                cwd=".",
                source_path="pyproject.toml",
            )
        ],
        package_managers=[
            PackageManager(
                name="uv",
                manifest_path="pyproject.toml",
                lockfile_path="uv.lock",
            )
        ],
    )

    plan = _runtime_plan(
        profile,
        [RuntimeRequirement(requirement_id="REQ-001", surface=RuntimeSurface.runtime)],
    )

    assert plan.blockers == []
    assert plan.checks[0].command.command == "pytest -q"


def test_manifest_command_without_locked_setup_is_an_explicit_blocker():
    profile = RepoProfile(
        commands=[
            RepoCommand(
                kind=CommandKind.test,
                command="npm test",
                cwd=".",
                source_path="package.json",
            )
        ],
        test_facilities=[
            ProfileTestFacility(
                name="vitest", package_path=".", source_path="package.json"
            )
        ],
    )

    plan = _runtime_plan(
        profile,
        [RuntimeRequirement(requirement_id="REQ-001", surface=RuntimeSurface.runtime)],
    )

    assert plan.checks == []
    assert "clean-runner setup is ambiguous" in plan.blockers[0].reason


def test_generated_workflow_installs_locked_dependencies_and_orchestrates_compose():
    profile = _profile()
    plan = _runtime_plan(
        profile,
        [
            RuntimeRequirement(
                requirement_id="REQ-001",
                surface=RuntimeSurface.service_container,
            )
        ],
    )

    workflow = render_github_actions_workflow(
        plan,
        profile,
        policy=RuntimeAcceptancePolicy(enabled=True),
    )

    assert 'run: "npm ci"' in workflow
    assert 'run: "docker compose -f compose.yaml up -d --wait"' in workflow
    assert 'run: "docker compose -f compose.yaml down -v"' in workflow
    assert "APDL_RUNTIME_HEAD_SHA" in workflow


def test_workflow_policy_and_yaml_scalars_are_explicit_safe_and_deterministic():
    command = 'printf "%s" "key: # value"'
    profile = _profile(command=command, cwd="on")
    plan = _runtime_plan(
        profile,
        [RuntimeRequirement(requirement_id="REQ-001", surface=RuntimeSurface.browser)],
    )

    with pytest.raises(WorkflowGenerationNotAuthorized):
        render_github_actions_workflow(
            plan,
            profile,
            policy=RuntimeAcceptancePolicy(),
        )

    policy = RuntimeAcceptancePolicy(enabled=True)
    first = render_github_actions_workflow(plan, profile, policy=policy)
    assert first == render_github_actions_workflow(plan, profile, policy=policy)
    assert '"on":' in first
    assert 'run: "printf \\"%s\\" \\"key: # value\\""' in first
    assert 'working-directory: "on"' in first
    assert "npm install" not in first
    assert 'run: "npm ci"' in first
    assert "docker compose" not in first
    assert "actions/upload-artifact@v4" in first
    assert "if-no-files-found: error" in first
    assert (
        'path: "on/apdl-runtime-evidence.json\\non/playwright-report/**\\non/test-results/**"'
        in first
    )

    with pytest.raises(ValidationError):
        RuntimeAcceptancePolicy(enabled=1)

    request = RuntimeAcceptanceRequest(enabled=True)
    assert request.enabled is True
    with pytest.raises(ValidationError):
        RuntimeAcceptanceRequest(workflow_changes_authorized=True)
    with pytest.raises(ValidationError):
        RuntimeAcceptanceRequest(generated_workflow_path=".github/workflows/ci.yml")
    with pytest.raises(ValidationError):
        RuntimeAcceptancePolicy(generated_workflow_path=".github/workflows/ci.yml")

    generated = build_runtime_acceptance_plan(
        profile, _verification_plan(), policy=policy
    )
    assert generated.generated_workflow is not None
    assert generated.generated_workflow.path == RUNTIME_ACCEPTANCE_WORKFLOW_PATH


def test_workflow_rejects_non_test_commands_and_mismatched_repository_identity():
    profile = _profile()
    plan = _runtime_plan(profile, _requirements())
    policy = RuntimeAcceptancePolicy(enabled=True)

    tampered = plan.model_copy(deep=True)
    tampered.checks[0].command = RuntimeCommand(
        command="invented test command", cwd=".", source_path="package.json"
    )
    with pytest.raises(ValueError, match="absent from RepoProfile"):
        render_github_actions_workflow(tampered, profile, policy=policy)

    wrong_profile = profile.model_copy(update={"repo": "acme/other"}, deep=True)
    with pytest.raises(ValueError, match="identity"):
        render_github_actions_workflow(plan, wrong_profile, policy=policy)

    changed_profile = profile.model_copy(update={"languages": ["Python"]}, deep=True)
    with pytest.raises(ValueError, match="provenance"):
        render_github_actions_workflow(plan, changed_profile, policy=policy)


def test_strict_runtime_ids_and_status_shapes_reject_ambiguous_evidence():
    with pytest.raises(ValidationError):
        RuntimeRequirement(requirement_id="REQ-BROWSER", surface=RuntimeSurface.browser)

    with pytest.raises(ValidationError):
        RuntimeArtifactExpectation(
            artifact_name="apdl-strict-evidence",
            evidence_kind=RuntimeEvidenceKind.structured_runtime,
            paths=["apdl-runtime-evidence.json"],
            requirement_ids=["REQ-001"],
            required=1,
        )

    with pytest.raises(ValidationError, match="artifact ID"):
        RuntimeArtifactObservation(
            artifact_name="apdl-browser-evidence",
            workflow_run_id=7,
            head_sha="head-new",
            status=RuntimeEvidenceStatus.observed,
            requirement_ids=["REQ-001"],
            files=[_artifact_file()],
        )
    with pytest.raises(ValidationError, match="cannot carry file evidence"):
        RuntimeArtifactObservation(
            artifact_name="apdl-browser-evidence",
            workflow_run_id=7,
            head_sha="head-new",
            status=RuntimeEvidenceStatus.unverified,
            requirement_ids=["REQ-001"],
            files=[_artifact_file()],
            unverified_reason="not usable",
        )


def test_exact_head_artifacts_never_change_github_ci_result():
    plan = _runtime_plan(
        _profile(),
        [RuntimeRequirement(requirement_id="REQ-001", surface=RuntimeSurface.browser)],
    )
    expectation = plan.checks[0].expected_artifacts[0]
    stale = RuntimeArtifactObservation(
        artifact_name=expectation.artifact_name,
        artifact_id=8,
        workflow_run_id=6,
        head_sha="head-old",
        status=RuntimeEvidenceStatus.observed,
        requirement_ids=expectation.requirement_ids,
        files=[
            _manifest_file(),
            _artifact_file("playwright-report/index.html"),
        ],
    )
    missing = RuntimeArtifactObservation(
        artifact_name=expectation.artifact_name,
        workflow_run_id=7,
        head_sha="head-new",
        status=RuntimeEvidenceStatus.unverified,
        requirement_ids=expectation.requirement_ids,
        unverified_reason="not uploaded",
    )

    assessment = assess_runtime_evidence(
        plan,
        [stale, missing],
        head_sha="head-new",
        external_ci_status=ExternalCIStatus.passed,
    )
    assert assessment.external_ci_status == "passed"
    assert assessment.requirements[0].status is RuntimeEvidenceStatus.unverified

    observed = RuntimeArtifactObservation(
        artifact_name=expectation.artifact_name,
        artifact_id=9,
        workflow_run_id=7,
        head_sha="head-new",
        status=RuntimeEvidenceStatus.observed,
        requirement_ids=expectation.requirement_ids,
        files=[
            _manifest_file(),
            _artifact_file("playwright-report/index.html"),
        ],
    )
    assessment = assess_runtime_evidence(
        plan,
        [observed],
        head_sha="head-new",
        external_ci_status=ExternalCIStatus.failed,
    )
    assert assessment.external_ci_status == "failed"
    assert assessment.requirements[0].status is RuntimeEvidenceStatus.observed

    blank_manifest = RuntimeArtifactObservation(
        artifact_name=expectation.artifact_name,
        artifact_id=10,
        workflow_run_id=8,
        head_sha="head-new",
        status=RuntimeEvidenceStatus.observed,
        requirement_ids=expectation.requirement_ids,
        files=[_artifact_file()],
    )
    blank_assessment = assess_runtime_evidence(
        plan,
        [blank_manifest],
        head_sha="head-new",
        external_ci_status=ExternalCIStatus.passed,
    )
    assert (
        blank_assessment.requirements[0].status
        is RuntimeEvidenceStatus.unverified
    )


def test_exact_head_assessment_rejects_unplanned_evidence_mapping():
    plan = _runtime_plan(
        _profile(),
        [RuntimeRequirement(requirement_id="REQ-001", surface=RuntimeSurface.browser)],
    )
    observation = RuntimeArtifactObservation(
        artifact_name=plan.checks[0].expected_artifacts[0].artifact_name,
        artifact_id=9,
        workflow_run_id=7,
        head_sha="head-new",
        status=RuntimeEvidenceStatus.observed,
        requirement_ids=["REQ-002"],
        files=[
            _manifest_file(),
            _artifact_file("playwright-report/index.html"),
        ],
    )

    with pytest.raises(ValueError, match="unplanned requirement"):
        assess_runtime_evidence(
            plan,
            [observation],
            head_sha="head-new",
            external_ci_status=ExternalCIStatus.passed,
        )


def test_job_log_evidence_is_exact_head_bounded_and_redaction_aware():
    evidence = RuntimeJobLogEvidence(
        workflow_run_id=7,
        job_id=70,
        job_name="runtime / browser",
        head_sha="head-new",
        text_excerpt="token=[REDACTED]",
        excerpt_byte_count=len("token=[REDACTED]".encode()),
        source_byte_count=200,
        truncated=True,
        redacted=True,
        github_url="https://github.test/acme/widgets/actions/runs/7/job/70",
    )

    assert evidence.schema_version == "runtime_job_log_evidence@1"
    with pytest.raises(ValidationError, match="excerpt_byte_count"):
        RuntimeJobLogEvidence.model_validate(
            {**evidence.model_dump(), "excerpt_byte_count": 1}
        )
    with pytest.raises(ValidationError):
        RuntimeJobLogEvidence.model_validate(
            {**evidence.model_dump(), "head_sha": "bad head"}
        )


def test_runtime_evidence_observation_is_append_only_exact_head_evidence():
    plan = _runtime_plan(
        _profile(),
        [RuntimeRequirement(requirement_id="REQ-001", surface=RuntimeSurface.browser)],
    )
    expectation = plan.checks[0].expected_artifacts[0]
    artifact = RuntimeArtifactObservation(
        artifact_name=expectation.artifact_name,
        artifact_id=9,
        workflow_run_id=7,
        head_sha="head-new",
        status=RuntimeEvidenceStatus.observed,
        requirement_ids=["REQ-001"],
        files=[
            _manifest_file(),
            _artifact_file("playwright-report/index.html"),
        ],
    )
    assessment = assess_runtime_evidence(
        plan,
        [artifact],
        head_sha="head-new",
        external_ci_status=ExternalCIStatus.unverified_external_ci,
    )
    log = RuntimeJobLogEvidence(
        workflow_run_id=7,
        job_id=70,
        job_name="runtime / browser",
        head_sha="head-new",
        text_excerpt="passed",
        excerpt_byte_count=6,
        source_byte_count=6,
        truncated=False,
        redacted=False,
        github_url="https://github.test/acme/widgets/actions/runs/7/job/70",
    )

    observation = RuntimeEvidenceObservation(
        observation_id="runtime_obs_" + "a" * 32,
        changeset_id="changeset-1",
        repository="acme/widgets",
        pr_number=17,
        head_sha="head-new",
        ci_observation_id="ciobs_" + "b" * 32,
        ci_evidence_hash="c" * 64,
        runtime_acceptance_plan_sha256=plan.evidence_hash(),
        observed_at=datetime.now(timezone.utc),
        artifacts=[artifact],
        job_logs=[log],
        assessment=assessment,
        collection_errors=[],
    )

    # Artifact presence is runtime evidence only; it cannot turn absent GitHub
    # CI into a passed external status.
    assert observation.assessment.external_ci_status == "unverified_external_ci"
    assert (
        observation.assessment.requirements[0].status is RuntimeEvidenceStatus.observed
    )

    repeated_payload = observation.model_dump(mode="json")
    repeated_payload["observation_id"] = "runtime_obs_" + "b" * 32
    repeated_payload["observed_at"] = "2026-01-02T00:00:00Z"
    repeated = RuntimeEvidenceObservation.model_validate_json(
        json.dumps(repeated_payload)
    )
    assert repeated.evidence_hash() == observation.evidence_hash()

    stale_payload = observation.model_dump(mode="json")
    stale_payload["job_logs"][0]["head_sha"] = "head-old"
    with pytest.raises(ValidationError, match="observation head SHA"):
        RuntimeEvidenceObservation.model_validate_json(json.dumps(stale_payload))

    duplicate_payload = observation.model_dump(mode="json")
    duplicate_payload["artifacts"].append(duplicate_payload["artifacts"][0])
    with pytest.raises(ValidationError, match="identities must be unique"):
        RuntimeEvidenceObservation.model_validate_json(json.dumps(duplicate_payload))

    naive_payload = observation.model_dump(mode="json")
    naive_payload["observed_at"] = "2026-01-01T00:00:00"
    with pytest.raises(ValidationError, match="timezone-aware"):
        RuntimeEvidenceObservation.model_validate_json(json.dumps(naive_payload))
