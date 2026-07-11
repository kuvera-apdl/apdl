"""Strict canonical schema for repository capabilities and uncertainty."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommandKind(str, Enum):
    format = "format"
    lint = "lint"
    typecheck = "typecheck"
    build = "build"
    test = "test"


class BranchProtectionStatus(str, Enum):
    protected = "protected"
    unprotected = "unprotected"
    unknown = "unknown"


class UncertaintyCode(str, Enum):
    conflicting_package_managers = "conflicting_package_managers"
    package_manager_unknown = "package_manager_unknown"
    malformed_manifest = "malformed_manifest"
    unresolved_dependency_versions = "unresolved_dependency_versions"
    branch_protection_unknown = "branch_protection_unknown"
    repository_tree_truncated = "repository_tree_truncated"
    unsupported_workspace_pattern = "unsupported_workspace_pattern"
    incomplete_remote_snapshot = "incomplete_remote_snapshot"


class Uncertainty(StrictModel):
    code: UncertaintyCode
    message: str
    paths: list[str] = Field(default_factory=list)


class PackageManager(StrictModel):
    name: str
    manifest_path: str
    lockfile_path: str | None = None
    declared_version: str | None = None


class PackageBoundary(StrictModel):
    path: str
    ecosystem: str
    name: str | None = None
    manifest_path: str


class WorkspaceBoundary(StrictModel):
    root: str
    ecosystem: str
    members: list[str] = Field(default_factory=list)
    source_path: str


class RepoCommand(StrictModel):
    kind: CommandKind
    command: str
    cwd: str
    source_path: str


class Dependency(StrictModel):
    name: str
    ecosystem: str
    package_path: str
    declared_constraint: str | None = None
    resolved_version: str | None = None
    source_path: str


class TestFacility(StrictModel):
    name: str
    package_path: str
    browser: bool = False
    source_path: str


class CodeSurface(StrictModel):
    kind: str
    path: str
    package_path: str = "."


class DeploymentTarget(StrictModel):
    kind: str
    path: str


class CIWorkflow(StrictModel):
    provider: Literal["github_actions", "gitlab", "circleci", "other"]
    path: str


class RepositoryInstruction(StrictModel):
    path: str
    scope: str
    content: str
    content_sha256: str
    truncated: bool = False


class BranchProtection(StrictModel):
    status: BranchProtectionStatus = BranchProtectionStatus.unknown
    source: str = "unavailable"


class RepoProfile(StrictModel):
    schema_version: Literal["repo_profile@1"] = "repo_profile@1"
    repo: str | None = None
    branch: str | None = None
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    package_managers: list[PackageManager] = Field(default_factory=list)
    lockfiles: list[str] = Field(default_factory=list)
    workspaces: list[WorkspaceBoundary] = Field(default_factory=list)
    packages: list[PackageBoundary] = Field(default_factory=list)
    commands: list[RepoCommand] = Field(default_factory=list)
    test_facilities: list[TestFacility] = Field(default_factory=list)
    routes: list[CodeSurface] = Field(default_factory=list)
    entrypoints: list[CodeSurface] = Field(default_factory=list)
    services: list[CodeSurface] = Field(default_factory=list)
    deployment_targets: list[DeploymentTarget] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)
    ci_workflows: list[CIWorkflow] = Field(default_factory=list)
    branch_protection: BranchProtection = Field(default_factory=BranchProtection)
    instructions: list[RepositoryInstruction] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    paths_truncated: bool = False
