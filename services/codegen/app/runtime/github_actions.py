"""Deterministic GitHub Actions renderer for explicitly authorized repositories."""

from __future__ import annotations

import hashlib
import json

from app.profiling.models import CommandKind, RepoProfile
from app.runtime.models import (
    GeneratedRuntimeWorkflowAttestation,
    RuntimeAcceptancePlan,
    RuntimeAcceptancePolicy,
    RuntimeCheck,
    RuntimeSurface,
)


class WorkflowGenerationNotAuthorized(PermissionError):
    """Raised unless repository policy explicitly authorizes workflow creation."""


def workflow_attestation_is_valid(
    attestation: GeneratedRuntimeWorkflowAttestation | None,
    *,
    plan: RuntimeAcceptancePlan | None,
    policy: RuntimeAcceptancePolicy,
) -> bool:
    """Validate the editor's exact workflow/plan binding for a gate exemption."""
    return bool(
        policy.workflow_changes_authorized
        and plan is not None
        and bool(plan.checks)
        and attestation is not None
        and plan.generated_workflow is not None
        and plan.generated_workflow.renderer == attestation.renderer
        and plan.generated_workflow.path == policy.generated_workflow_path
        and attestation.path == plan.generated_workflow.path
        and attestation.content_sha256 == plan.generated_workflow.content_sha256
        and attestation.runtime_acceptance_plan_sha256 == plan.evidence_hash()
    )


def _yaml_scalar(value: str) -> str:
    """Render a YAML-safe double-quoted scalar with deterministic escaping."""
    return json.dumps(value, ensure_ascii=True)


def _profile_sha256(profile: RepoProfile) -> str:
    payload = json.dumps(
        profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _manager_for(check: RuntimeCheck, profile: RepoProfile):
    values = [
        manager
        for manager in profile.package_managers
        if manager.manifest_path == check.command.source_path
    ]
    return values[0] if len(values) == 1 else None


def _setup_steps(check: RuntimeCheck, profile: RepoProfile) -> list[str]:
    manager = _manager_for(check, profile)
    if manager is None:
        return []
    cwd = _yaml_scalar(check.command.cwd)
    commands: dict[str, tuple[str, ...]] = {
        "npm": ("npm ci",),
        "pnpm": ("corepack enable", "pnpm install --frozen-lockfile"),
        "yarn": ("corepack enable", "yarn install --immutable"),
        "bun": ("bun install --frozen-lockfile",),
        "uv": (
            "uv sync --frozen",
            'echo "$PWD/.venv/bin" >> "$GITHUB_PATH"',
        ),
        "poetry": (
            "poetry install --no-interaction --sync",
            'echo "$(poetry env info --path)/bin" >> "$GITHUB_PATH"',
        ),
        "pdm": ("pdm sync --frozen-lockfile",),
    }
    lines: list[str] = []
    if manager.name == "bun":
        lines.extend(
            [
                "      - name: Set up Bun",
                "        uses: oven-sh/setup-bun@v2",
            ]
        )
    elif manager.name == "uv":
        lines.extend(
            [
                "      - name: Set up uv",
                "        uses: astral-sh/setup-uv@v5",
            ]
        )
    elif manager.name in {"poetry", "pdm"}:
        lines.extend(
            [
                f"      - name: Install {manager.name}",
                f"        run: {_yaml_scalar(f'pipx install {manager.name}')}",
            ]
        )
    if manager.name in commands:
        lines.extend(
            [
                "      - name: Install locked dependencies",
                "        run: " + _yaml_scalar("\n".join(commands[manager.name])),
                f"        working-directory: {cwd}",
            ]
        )
    if check.surface is RuntimeSurface.browser and any(
        facility.browser
        and facility.package_path == check.command.cwd
        and "playwright" in facility.name.lower()
        for facility in profile.test_facilities
    ):
        lines.extend(
            [
                "      - name: Install Playwright browsers",
                "        run: \"npx playwright install --with-deps\"",
                f"        working-directory: {cwd}",
            ]
        )
    if check.surface is RuntimeSurface.service_container:
        for path in check.service_container_paths:
            lines.extend(
                [
                    "      - name: Start declared service containers",
                    "        run: "
                    + _yaml_scalar(f"docker compose -f {path} up -d --wait"),
                ]
            )
    return lines


def _upload_path(cwd: str, path: str) -> str:
    return path if cwd == "." else f"{cwd}/{path}"


def render_github_actions_workflow(
    plan: RuntimeAcceptancePlan,
    profile: RepoProfile,
    *,
    policy: RuntimeAcceptancePolicy,
) -> str:
    """Render exact profile commands; never infer setup, install, or test steps."""
    if policy.workflow_changes_authorized is not True:
        raise WorkflowGenerationNotAuthorized(
            "repository policy did not authorize GitHub workflow generation"
        )
    if not plan.checks:
        raise ValueError("runtime plan has no executable checks")
    if (plan.repo, plan.branch) != (profile.repo, profile.branch):
        raise ValueError("runtime plan repository identity does not match RepoProfile")
    if plan.repo_profile_sha256 != _profile_sha256(profile):
        raise ValueError("runtime plan RepoProfile provenance does not match")

    known_commands = {
        (command.command, command.cwd, command.source_path)
        for command in profile.commands
        if command.kind is CommandKind.test
    }
    lines = [
        f"name: {_yaml_scalar('APDL Runtime Acceptance')}",
        "",
        '"on":',
        "  pull_request:",
        "",
        "permissions:",
        "  contents: read",
        "",
        "concurrency:",
        '  group: "apdl-runtime-${{ github.event.pull_request.head.sha }}"',
        "  cancel-in-progress: true",
        "",
        "jobs:",
    ]
    for check in sorted(plan.checks, key=lambda item: item.check_id):
        command_key = (
            check.command.command,
            check.command.cwd,
            check.command.source_path,
        )
        if command_key not in known_commands:
            raise ValueError(
                f"runtime check {check.check_id} uses a command absent from RepoProfile"
            )
        lines.extend(
            [
                f"  {check.check_id.replace('-', '_')}:",
                "    runs-on: ubuntu-latest",
                "    timeout-minutes: 20",
                "    steps:",
                "      - name: Checkout exact PR head",
                "        uses: actions/checkout@v4",
                "        with:",
                '          ref: "${{ github.event.pull_request.head.sha }}"',
            ]
        )
        lines.extend(_setup_steps(check, profile))
        lines.extend(
            [
                "      - name: Run repository-declared runtime check",
                f"        run: {_yaml_scalar(check.command.command)}",
                f"        working-directory: {_yaml_scalar(check.command.cwd)}",
                "        env:",
                '          APDL_RUNTIME_HEAD_SHA: "${{ github.event.pull_request.head.sha }}"',
            ]
        )
        for expectation in sorted(
            check.expected_artifacts, key=lambda item: item.artifact_name
        ):
            lines.extend(
                [
                    "      - name: "
                    + _yaml_scalar(f"Upload {expectation.artifact_name}"),
                    "        if: always()",
                    "        uses: actions/upload-artifact@v4",
                    "        with:",
                    f"          name: {_yaml_scalar(expectation.artifact_name)}",
                    "          if-no-files-found: "
                    + ("error" if expectation.required else "warn"),
                    "          path: "
                    + _yaml_scalar(
                        "\n".join(
                            _upload_path(check.command.cwd, path)
                            for path in expectation.paths
                        )
                    ),
                ]
            )
        if check.surface is RuntimeSurface.service_container:
            for path in reversed(check.service_container_paths):
                lines.extend(
                    [
                        "      - name: Stop declared service containers",
                        "        if: always()",
                        "        run: "
                        + _yaml_scalar(f"docker compose -f {path} down -v"),
                    ]
                )
    rendered = "\n".join(lines) + "\n"
    if plan.generated_workflow is not None:
        expected = plan.generated_workflow
        actual_sha256 = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        if expected.renderer != "apdl_github_actions_runtime@1":
            raise ValueError("runtime workflow renderer binding is unsupported")
        if expected.path != policy.generated_workflow_path:
            raise ValueError("runtime workflow path does not match the plan binding")
        if expected.content_sha256 != actual_sha256:
            raise ValueError("runtime workflow bytes do not match the plan binding")
    return rendered
