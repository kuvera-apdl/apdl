"""Derive runtime acceptance work solely from canonical RepoProfile facts."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from app.models.observations import ExternalCIStatus
from app.profiling.models import CommandKind, RepoCommand, RepoProfile
from app.runtime.models import (
    GeneratedRuntimeWorkflowExpectation,
    RequirementRuntimeEvidence,
    RuntimeAcceptancePlan,
    RuntimeAcceptancePolicy,
    RuntimeArtifactExpectation,
    RuntimeArtifactObservation,
    RuntimeBlocker,
    RuntimeCheck,
    RuntimeCommand,
    RuntimeEvidenceAssessment,
    RuntimeEvidenceKind,
    RuntimeEvidenceManifest,
    RuntimeEvidenceStatus,
    RuntimeRequirement,
    RuntimeSurface,
)
from app.verification.models import VerificationPlan, VerificationSurface

_ARTIFACTS: dict[RuntimeSurface, tuple[str, RuntimeEvidenceKind, tuple[str, ...]]] = {
    RuntimeSurface.browser: (
        "apdl-browser-evidence",
        RuntimeEvidenceKind.browser_report,
        (
            "apdl-runtime-evidence.json",
            "playwright-report/**",
            "test-results/**",
        ),
    ),
    RuntimeSurface.api: (
        "apdl-api-evidence",
        RuntimeEvidenceKind.request_trace,
        ("apdl-runtime-evidence.json", "request-traces/**"),
    ),
    RuntimeSurface.service_container: (
        "apdl-service-evidence",
        RuntimeEvidenceKind.server_log,
        ("apdl-runtime-evidence.json", "runtime-logs/**"),
    ),
    RuntimeSurface.runtime: (
        "apdl-runtime-evidence",
        RuntimeEvidenceKind.structured_runtime,
        ("apdl-runtime-evidence.json",),
    ),
}

_SURFACE_MAPPING: dict[VerificationSurface, RuntimeSurface] = {
    VerificationSurface.ui: RuntimeSurface.browser,
    VerificationSurface.api: RuntimeSurface.api,
    VerificationSurface.database: RuntimeSurface.service_container,
    # The remaining verification surfaces do not prove a more specific runtime
    # harness. Route them to a repository-declared runtime command rather than
    # inventing browser, API, or service-container capabilities.
    VerificationSurface.general: RuntimeSurface.runtime,
    VerificationSurface.sdk: RuntimeSurface.runtime,
    VerificationSurface.analytics: RuntimeSurface.runtime,
    VerificationSurface.security: RuntimeSurface.runtime,
    VerificationSurface.billing: RuntimeSurface.runtime,
    VerificationSurface.concurrency: RuntimeSurface.runtime,
}


def _canonical_sha256(value: RepoProfile | VerificationPlan) -> str:
    payload = json.dumps(
        value.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def derive_runtime_requirements(
    verification_plan: VerificationPlan,
) -> list[RuntimeRequirement]:
    """Map the canonical verification plan to deterministic runtime surfaces.

    Policy checks intentionally collapse to one runtime requirement per
    ``(requirement_id, runtime surface)``. The verification plan remains the
    source of requirement identity; this layer never classifies prose again.
    """
    pairs = {
        (item.requirement_id, _SURFACE_MAPPING[item.surface])
        for item in verification_plan.items
    }
    return [
        RuntimeRequirement(requirement_id=requirement_id, surface=surface)
        for requirement_id, surface in sorted(
            pairs, key=lambda pair: (pair[0], pair[1].value)
        )
    ]


def _test_commands(profile: RepoProfile) -> list[RepoCommand]:
    return sorted(
        (command for command in profile.commands if command.kind is CommandKind.test),
        key=lambda command: (command.cwd, command.source_path, command.command),
    )


def _candidate_cwds(profile: RepoProfile, surface: RuntimeSurface) -> set[str]:
    if surface is RuntimeSurface.browser:
        return {
            facility.package_path
            for facility in profile.test_facilities
            if facility.browser
        }
    if surface is RuntimeSurface.api:
        return {route.package_path for route in profile.routes}
    if surface is RuntimeSurface.runtime:
        return {
            item.package_path
            for item in [
                *profile.entrypoints,
                *profile.services,
                *profile.routes,
                *profile.test_facilities,
            ]
        }
    if surface is RuntimeSurface.service_container:
        return {
            target.path.rsplit("/", 1)[0] if "/" in target.path else "."
            for target in profile.deployment_targets
            if target.kind == "docker_compose"
        }
    return set()


def _surface_evidence_paths(profile: RepoProfile, surface: RuntimeSurface) -> list[str]:
    if surface is RuntimeSurface.browser:
        return sorted(
            {
                facility.source_path
                for facility in profile.test_facilities
                if facility.browser
            }
        )
    if surface is RuntimeSurface.api:
        return sorted({route.path for route in profile.routes})
    if surface is RuntimeSurface.service_container:
        return sorted(
            {
                target.path
                for target in profile.deployment_targets
                if target.kind == "docker_compose"
            }
        )
    return sorted({item.path for item in [*profile.entrypoints, *profile.services]})


def _command_for_surface(
    profile: RepoProfile, surface: RuntimeSurface
) -> RepoCommand | None:
    commands = _test_commands(profile)
    candidate_cwds = _candidate_cwds(profile, surface)
    matching = [command for command in commands if command.cwd in candidate_cwds]
    if matching:
        return matching[0]
    # A root test command is a repository-declared umbrella command, so it may
    # exercise a nested surface without APDL inventing a package command.
    return next((command for command in commands if command.cwd == "."), None)


def _surface_available(profile: RepoProfile, surface: RuntimeSurface) -> bool:
    if surface is RuntimeSurface.browser:
        return any(facility.browser for facility in profile.test_facilities)
    if surface is RuntimeSurface.api:
        return bool(profile.routes)
    if surface is RuntimeSurface.service_container:
        return any(
            target.kind == "docker_compose" for target in profile.deployment_targets
        )
    return bool(
        profile.entrypoints
        or profile.services
        or profile.routes
        or profile.test_facilities
        or _test_commands(profile)
    )


def _setup_blocker(profile: RepoProfile, command: RepoCommand) -> str | None:
    """Require reproducible clean-runner setup facts for manifest test commands."""
    source_name = command.source_path.rsplit("/", 1)[-1]
    managed_sources = {
        "package.json": {"npm", "pnpm", "yarn", "bun"},
        "pyproject.toml": {"uv", "poetry", "pdm"},
        "go.mod": {"go_modules"},
        "Cargo.toml": {"cargo"},
    }
    supported = managed_sources.get(source_name)
    if supported is None:
        return None
    managers = [
        manager
        for manager in profile.package_managers
        if manager.manifest_path == command.source_path
    ]
    if len(managers) != 1:
        return (
            f"RepoProfile must expose exactly one package manager for "
            f"{command.source_path}; clean-runner setup is ambiguous."
        )
    manager = managers[0]
    if manager.name not in supported:
        return (
            f"Package manager {manager.name!r} has no deterministic runtime "
            "setup adapter."
        )
    if manager.lockfile_path is None:
        return (
            f"Package manager {manager.name!r} has no lockfile; runtime setup "
            "cannot be frozen to exact dependencies."
        )
    return None


def _check_id(
    surface: RuntimeSurface, command: RepoCommand, requirement_ids: list[str]
) -> str:
    payload = json.dumps(
        {
            "command": command.command,
            "cwd": command.cwd,
            "requirements": requirement_ids,
            "surface": surface.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "runtime_" + hashlib.sha256(payload).hexdigest()[:16]


def plan_runtime_acceptance(
    profile: RepoProfile,
    requirements: list[RuntimeRequirement],
    *,
    source_ledger_sha256: str,
    verification_plan_sha256: str,
) -> RuntimeAcceptancePlan:
    """Build checks from explicit requirements with caller-supplied provenance."""
    requirement_pairs = [
        (requirement.requirement_id, requirement.surface)
        for requirement in requirements
    ]
    if len(requirement_pairs) != len(set(requirement_pairs)):
        raise ValueError("runtime requirement surfaces must be unique")

    grouped: dict[RuntimeSurface, list[str]] = defaultdict(list)
    for requirement in requirements:
        grouped[requirement.surface].append(requirement.requirement_id)

    checks: list[RuntimeCheck] = []
    blockers: list[RuntimeBlocker] = []
    for surface in sorted(grouped, key=lambda value: value.value):
        requirement_ids = sorted(set(grouped[surface]))
        evidence_paths = _surface_evidence_paths(profile, surface)
        if not _surface_available(profile, surface):
            for requirement_id in requirement_ids:
                blockers.append(
                    RuntimeBlocker(
                        requirement_id=requirement_id,
                        surface=surface,
                        reason=(
                            f"RepoProfile has no {surface.value} capability evidence; "
                            "runtime verification remains unverified."
                        ),
                        evidence_paths=evidence_paths,
                    )
                )
            continue
        command = _command_for_surface(profile, surface)
        if command is None:
            for requirement_id in requirement_ids:
                blockers.append(
                    RuntimeBlocker(
                        requirement_id=requirement_id,
                        surface=surface,
                        reason=(
                            "RepoProfile exposes no repository-declared test command "
                            f"for the {surface.value} surface."
                        ),
                        evidence_paths=evidence_paths,
                    )
                )
            continue
        setup_blocker = _setup_blocker(profile, command)
        if setup_blocker is not None:
            for requirement_id in requirement_ids:
                blockers.append(
                    RuntimeBlocker(
                        requirement_id=requirement_id,
                        surface=surface,
                        reason=setup_blocker,
                        evidence_paths=sorted(
                            set(evidence_paths) | {command.source_path}
                        ),
                    )
                )
            continue
        artifact_name, evidence_kind, paths = _ARTIFACTS[surface]
        expectation = RuntimeArtifactExpectation(
            artifact_name=artifact_name,
            evidence_kind=evidence_kind,
            paths=sorted(paths),
            requirement_ids=requirement_ids,
        )
        checks.append(
            RuntimeCheck(
                check_id=_check_id(surface, command, requirement_ids),
                surface=surface,
                requirement_ids=requirement_ids,
                command=RuntimeCommand(
                    command=command.command,
                    cwd=command.cwd,
                    source_path=command.source_path,
                ),
                service_container_paths=(
                    evidence_paths
                    if surface is RuntimeSurface.service_container
                    else []
                ),
                expected_artifacts=[expectation],
            )
        )
    return RuntimeAcceptancePlan(
        source_ledger_sha256=source_ledger_sha256,
        repo_profile_sha256=_canonical_sha256(profile),
        verification_plan_sha256=verification_plan_sha256,
        repo=profile.repo,
        branch=profile.branch,
        checks=sorted(checks, key=lambda check: (check.surface.value, check.check_id)),
        blockers=sorted(
            blockers,
            key=lambda blocker: (blocker.requirement_id, blocker.surface.value),
        ),
    )


def build_runtime_acceptance_plan(
    profile: RepoProfile,
    verification_plan: VerificationPlan,
    *,
    policy: RuntimeAcceptancePolicy | None = None,
) -> RuntimeAcceptancePlan:
    """Build the canonical provenance-bound runtime plan from Phase 5 output."""
    if verification_plan.repo_profile_schema_version != profile.schema_version:
        raise ValueError(
            "verification plan and RepoProfile schema versions do not match"
        )
    plan = plan_runtime_acceptance(
        profile,
        derive_runtime_requirements(verification_plan),
        source_ledger_sha256=verification_plan.source_ledger_sha256,
        verification_plan_sha256=_canonical_sha256(verification_plan),
    )
    if policy is None or not policy.workflow_changes_authorized or not plan.checks:
        return plan

    # Import lazily to keep the planner/model layer independently importable.
    # The renderer does not include this expectation in its bytes, so the hash
    # can be computed first and then bound into the final plan without a cycle.
    from app.runtime.github_actions import render_github_actions_workflow

    rendered = render_github_actions_workflow(plan, profile, policy=policy)
    expectation = GeneratedRuntimeWorkflowExpectation(
        path=policy.generated_workflow_path,
        content_sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    )
    return RuntimeAcceptancePlan.model_validate(
        {
            **plan.model_dump(mode="python"),
            "generated_workflow": expectation.model_dump(mode="python"),
        }
    )


def assess_runtime_evidence(
    plan: RuntimeAcceptancePlan,
    observations: list[RuntimeArtifactObservation],
    *,
    head_sha: str,
    external_ci_status: ExternalCIStatus,
) -> RuntimeEvidenceAssessment:
    """Assess artifacts without altering GitHub's authoritative CI result."""
    expected_by_requirement: dict[str, set[str]] = defaultdict(set)
    expectation_by_artifact: dict[str, RuntimeArtifactExpectation] = {}
    for check in plan.checks:
        for expectation in check.expected_artifacts:
            expectation_by_artifact[expectation.artifact_name] = expectation
            if not expectation.required:
                continue
            for requirement_id in expectation.requirement_ids:
                expected_by_requirement[requirement_id].add(expectation.artifact_name)

    exact_head_observations = [
        observation for observation in observations if observation.head_sha == head_sha
    ]
    observation_keys = [
        (
            observation.workflow_run_id,
            observation.artifact_id,
            observation.artifact_name,
        )
        for observation in exact_head_observations
    ]
    if len(observation_keys) != len(set(observation_keys)):
        raise ValueError("runtime artifact observations must have unique identities")

    observed_pairs: set[tuple[str, str]] = set()
    for observation in exact_head_observations:
        expectation = expectation_by_artifact.get(observation.artifact_name)
        if expectation is None:
            raise ValueError(
                f"runtime artifact {observation.artifact_name!r} is not in the plan"
            )
        expected_requirements = set(expectation.requirement_ids)
        if not set(observation.requirement_ids) <= expected_requirements:
            raise ValueError(
                "runtime artifact observation refers to an unplanned requirement"
            )
        if observation.status is not RuntimeEvidenceStatus.observed:
            continue
        manifests = [
            file
            for file in observation.files
            if file.path.rsplit("/", 1)[-1] == "apdl-runtime-evidence.json"
        ]
        if len(manifests) != 1:
            continue
        manifest_file = manifests[0]
        if (
            manifest_file.binary
            or manifest_file.redacted
            or manifest_file.text_excerpt is None
            or len(manifest_file.text_excerpt.encode("utf-8"))
            != manifest_file.byte_count
        ):
            continue
        try:
            manifest = RuntimeEvidenceManifest.model_validate_json(
                manifest_file.text_excerpt
            )
        except ValueError:
            continue
        if manifest.head_sha != head_sha:
            continue
        files_by_path = {file.path: file for file in observation.files}
        for result in manifest.requirements:
            if result.requirement_id not in observation.requirement_ids:
                continue
            if any(
                evidence.path not in files_by_path
                or files_by_path[evidence.path].content_sha256
                != evidence.content_sha256
                for evidence in result.evidence_files
            ):
                continue
            observed_pairs.add(
                (observation.artifact_name, result.requirement_id)
            )

    blockers_by_requirement: dict[str, list[str]] = defaultdict(list)
    for blocker in plan.blockers:
        blockers_by_requirement[blocker.requirement_id].append(blocker.reason)
    requirement_ids = sorted(
        {
            *expected_by_requirement,
            *blockers_by_requirement,
            *(item for check in plan.checks for item in check.requirement_ids),
        }
    )
    results: list[RequirementRuntimeEvidence] = []
    for requirement_id in requirement_ids:
        expected = expected_by_requirement.get(requirement_id, set())
        present = sorted(
            name for name in expected if (name, requirement_id) in observed_pairs
        )
        missing = sorted(expected - set(present))
        blockers = blockers_by_requirement.get(requirement_id, [])
        if expected and not missing and not blockers:
            results.append(
                RequirementRuntimeEvidence(
                    requirement_id=requirement_id,
                    status=RuntimeEvidenceStatus.observed,
                    artifact_names=present,
                )
            )
        else:
            reasons = list(blockers)
            if missing or not expected:
                reasons.append(
                    "Required GitHub Actions artifacts were not observed for the "
                    f"exact PR head: {', '.join(missing) or 'none planned'}."
                )
            results.append(
                RequirementRuntimeEvidence(
                    requirement_id=requirement_id,
                    status=RuntimeEvidenceStatus.unverified,
                    artifact_names=present,
                    reason=" ".join(reasons),
                )
            )
    # Pydantic validates the canonical external-CI value. It is copied exactly;
    # artifact presence or absence never promotes or demotes GitHub's result.
    return RuntimeEvidenceAssessment(
        head_sha=head_sha,
        external_ci_status=external_ci_status,
        requirements=results,
    )
