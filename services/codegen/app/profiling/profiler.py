"""Merge ecosystem fragments into one deterministic strict RepoProfile."""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any

from app.profiling.adapters import ADAPTERS, ProfileFragment
from app.profiling.models import (
    BranchProtection,
    BranchProtectionStatus,
    CIWorkflow,
    CodeSurface,
    CommandKind,
    DeploymentTarget,
    RepoCommand,
    RepoProfile,
    RepositoryInstruction,
    Uncertainty,
    UncertaintyCode,
)

_EXCLUDED = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        ".next",
        "target",
        "vendor",
        "coverage",
        "__pycache__",
    }
)
_MAX_PATHS = 5000
_MAX_INSTRUCTION_CHARS = 20_000


def _paths(root: Path) -> tuple[list[str], bool]:
    result: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in _EXCLUDED for part in rel.parts):
            continue
        result.append(rel.as_posix())
    result.sort()
    return result[:_MAX_PATHS], len(result) > _MAX_PATHS


def _unique(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        key = (
            json.dumps(value.model_dump(mode="json"), sort_keys=True)
            if hasattr(value, "model_dump")
            else str(value)
        )
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _generic(root: Path, paths: list[str]) -> tuple[list, list, list, list, list, list]:
    ci: list[CIWorkflow] = []
    instructions: list[RepositoryInstruction] = []
    deployments: list[DeploymentTarget] = []
    services: list[CodeSurface] = []
    protected: set[str] = set()
    frameworks: list[str] = []
    for path in paths:
        name = Path(path).name
        if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
            ci.append(CIWorkflow(provider="github_actions", path=path))
            protected.add(path)
        elif path == ".gitlab-ci.yml":
            ci.append(CIWorkflow(provider="gitlab", path=path))
        elif path == ".circleci/config.yml":
            ci.append(CIWorkflow(provider="circleci", path=path))
        if name == "AGENTS.md":
            scope = Path(path).parent.as_posix() or "."
            try:
                raw = (root / path).read_bytes()
            except OSError:
                raw = b""
            content = raw.decode("utf-8", "replace")
            instructions.append(
                RepositoryInstruction(
                    path=path,
                    scope=scope,
                    content=content[:_MAX_INSTRUCTION_CHARS],
                    content_sha256=hashlib.sha256(raw).hexdigest(),
                    truncated=len(content) > _MAX_INSTRUCTION_CHARS,
                )
            )
        if name == "Dockerfile" or name.startswith("Dockerfile."):
            deployments.append(DeploymentTarget(kind="docker", path=path))
        elif name in {
            "docker-compose.yml",
            "docker-compose.yaml",
            "compose.yml",
            "compose.yaml",
        }:
            deployments.append(DeploymentTarget(kind="docker_compose", path=path))
        elif name in {
            "vercel.json",
            "fly.toml",
            "railway.json",
            "serverless.yml",
            "serverless.yaml",
        }:
            deployments.append(DeploymentTarget(kind=name.split(".")[0], path=path))
        elif path.startswith(("k8s/", "kubernetes/", "helm/")) and path.endswith(
            (".yml", ".yaml")
        ):
            deployments.append(DeploymentTarget(kind="kubernetes", path=path))
        elif path.endswith(".tf"):
            deployments.append(DeploymentTarget(kind="terraform", path=path))
        if any(
            segment
            in {"auth", "security", "migrations", "infra", "deploy", "deployment"}
            for segment in Path(path).parts
        ):
            protected.add(path)
        if name in {
            "docker-compose.yml",
            "docker-compose.yaml",
            "compose.yml",
            "compose.yaml",
        }:
            services.append(CodeSurface(kind="compose_services", path=path))
    return ci, instructions, deployments, services, sorted(protected), frameworks


def _generic_commands(root: Path, paths: list[str]) -> list[RepoCommand]:
    commands: list[RepoCommand] = []
    for path in paths:
        if Path(path).name not in {"Makefile", "makefile"}:
            continue
        try:
            text = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        targets = {
            match.group(1)
            for match in re.finditer(r"(?m)^([A-Za-z0-9_.-]+):(?:\s|$)", text)
        }
        cwd = Path(path).parent.as_posix() or "."
        for target, kind in (
            ("format", CommandKind.format),
            ("fmt", CommandKind.format),
            ("lint", CommandKind.lint),
            ("typecheck", CommandKind.typecheck),
            ("build", CommandKind.build),
            ("test", CommandKind.test),
        ):
            if target in targets:
                commands.append(
                    RepoCommand(
                        kind=kind,
                        command=f"make {target}",
                        cwd=cwd,
                        source_path=path,
                    )
                )
    return commands


def profile_repository(
    root: Path,
    *,
    repo: str | None = None,
    branch: str | None = None,
    branch_protection: BranchProtection | None = None,
    paths_truncated: bool | None = None,
) -> RepoProfile:
    """Profile a checkout/snapshot without inferred ecosystem fallbacks."""
    paths, local_truncated = _paths(root)
    truncated = local_truncated if paths_truncated is None else paths_truncated
    fragments: list[ProfileFragment] = [
        adapter.profile(root, paths) for adapter in ADAPTERS if adapter.detect(paths)
    ]
    ci, instructions, deployments, services, protected, generic_frameworks = _generic(
        root, paths
    )
    generic_commands = _generic_commands(root, paths)
    uncertainties: list[Uncertainty] = [
        item for fragment in fragments for item in fragment.uncertainties
    ]
    protection = branch_protection or BranchProtection()
    if protection.status is BranchProtectionStatus.unknown:
        uncertainties.append(
            Uncertainty(
                code=UncertaintyCode.branch_protection_unknown,
                message="Branch-protection status was not available.",
                paths=[],
            )
        )
    if truncated:
        uncertainties.append(
            Uncertainty(
                code=UncertaintyCode.repository_tree_truncated,
                message="Repository path inventory was truncated.",
                paths=[],
            )
        )
    lockfiles = sorted(
        set(value for fragment in fragments for value in fragment.lockfiles)
    )
    packages = _unique([value for fragment in fragments for value in fragment.packages])
    package_services = [
        CodeSurface(
            kind="package_service",
            path=package.manifest_path,
            package_path=package.path,
        )
        for package in packages
        if Path(package.path).parts
        and Path(package.path).parts[0] in {"services", "apps"}
    ]
    protected_paths = sorted(
        {
            *protected,
            *lockfiles,
            *(workflow.path for workflow in ci),
            *(target.path for target in deployments),
        }
    )
    profile = RepoProfile(
        repo=repo,
        branch=branch,
        languages=sorted(
            set(value for fragment in fragments for value in fragment.languages)
        ),
        frameworks=sorted(
            set(
                [
                    *generic_frameworks,
                    *(value for fragment in fragments for value in fragment.frameworks),
                ]
            )
        ),
        package_managers=_unique(
            [value for fragment in fragments for value in fragment.package_managers]
        ),
        lockfiles=lockfiles,
        workspaces=_unique(
            [value for fragment in fragments for value in fragment.workspaces]
        ),
        packages=packages,
        commands=_unique(
            [
                *generic_commands,
                *(value for fragment in fragments for value in fragment.commands),
            ]
        ),
        test_facilities=_unique(
            [value for fragment in fragments for value in fragment.test_facilities]
        ),
        routes=_unique([value for fragment in fragments for value in fragment.routes]),
        entrypoints=_unique(
            [value for fragment in fragments for value in fragment.entrypoints]
        ),
        services=_unique(
            [
                *services,
                *package_services,
                *(value for fragment in fragments for value in fragment.services),
            ]
        ),
        deployment_targets=_unique(deployments),
        dependencies=_unique(
            [value for fragment in fragments for value in fragment.dependencies]
        ),
        ci_workflows=_unique(ci),
        branch_protection=protection,
        instructions=_unique(instructions),
        protected_paths=protected_paths,
        uncertainties=_unique(uncertainties),
        paths=paths,
        paths_truncated=truncated,
    )
    return profile


def render_profile(profile: RepoProfile) -> str:
    """Compact deterministic profile for model prompts."""
    payload = profile.model_dump(mode="json", exclude={"paths"})
    payload["path_sample"] = profile.paths[:200]
    return json.dumps(payload, indent=2, sort_keys=True)
