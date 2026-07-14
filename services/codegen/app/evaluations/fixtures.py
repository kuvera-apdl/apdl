"""Materialize deterministic synthetic mutations into tiny fixture repositories."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.evaluations.models import (
    EvaluationCase,
    HarnessObservation,
    StrictModel,
    canonical_sha256,
)


class FixtureAssertion(StrictModel):
    assertion_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    path: str = Field(min_length=1)
    contains: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("fixture assertion paths must be normalized relative paths")
        return value


class FixtureManifest(StrictModel):
    schema_version: Literal["evaluation_fixture@1"] = "evaluation_fixture@1"
    fixture_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    baseline_path: Literal["repository"] = "repository"
    mutation_patch: Literal["mutation.patch"] = "mutation.patch"
    assertions: list[FixtureAssertion] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_assertions(self) -> FixtureManifest:
        ids = [assertion.assertion_id for assertion in self.assertions]
        if len(ids) != len(set(ids)):
            raise ValueError("fixture assertion ids must be unique")
        return self


@dataclass(frozen=True)
class MaterializedFixture:
    fixture_id: str
    fixture_sha256: str
    workspace: Path
    baseline_tree_sha256: str
    mutation_commit_sha: str


def load_fixture_manifest(fixture_dir: Path) -> FixtureManifest:
    return FixtureManifest.model_validate_json(
        (fixture_dir / "fixture.json").read_text(encoding="utf-8")
    )


def fixture_sha256(fixture_dir: Path) -> str:
    """Bind a case to every committed fixture byte using stable relative paths."""
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in fixture_dir.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    for path in files:
        relative = path.relative_to(fixture_dir).as_posix().encode()
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _workspace_tree_sha256(workspace: Path) -> str:
    """Hash every candidate-tree entry without following untrusted symlinks."""
    digest = hashlib.sha256()
    entries = sorted(
        path
        for path in workspace.rglob("*")
        if ".git" not in path.relative_to(workspace).parts
        and "__pycache__" not in path.relative_to(workspace).parts
    )
    for path in entries:
        relative = path.relative_to(workspace).as_posix().encode()
        if path.is_symlink():
            kind = b"symlink"
            payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
        elif path.is_file():
            kind = b"file"
            payload = path.read_bytes()
        elif path.is_dir():
            kind = b"directory"
            payload = b""
        else:
            kind = b"special"
            payload = b""
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(kind).to_bytes(8, "big"))
        digest.update(kind)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _assertion_results(
    workspace: Path,
    manifest: FixtureManifest,
) -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []
    resolved_workspace = workspace.resolve()
    for assertion in manifest.assertions:
        try:
            target = (workspace / assertion.path).resolve()
        except (OSError, RuntimeError):
            results.append((assertion.assertion_id, False))
            continue
        if target != resolved_workspace and resolved_workspace not in target.parents:
            results.append((assertion.assertion_id, False))
            continue
        try:
            passed = target.is_file() and assertion.contains in target.read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeDecodeError):
            passed = False
        results.append((assertion.assertion_id, passed))
    return results


def run_fixture_harness(
    materialized: MaterializedFixture,
    manifest: FixtureManifest,
) -> HarnessObservation:
    results = _assertion_results(materialized.workspace, manifest)
    # The visible assertions explain the intended repair.  This sealed tree
    # identity is the actual acceptance boundary: comments containing the
    # expected snippet, unrelated edits, or generated files cannot satisfy it.
    # The baseline digest never crosses into the candidate invocation/image.
    final_tree_sha256 = _workspace_tree_sha256(materialized.workspace)
    results.append(
        ("sealed-baseline-tree", final_tree_sha256 == materialized.baseline_tree_sha256)
    )
    failing = [assertion_id for assertion_id, passed in results if not passed]
    evidence_sha = canonical_sha256(
        {
            "fixture_id": materialized.fixture_id,
            "fixture_sha256": materialized.fixture_sha256,
            "expected_tree_sha256": materialized.baseline_tree_sha256,
            "observed_tree_sha256": final_tree_sha256,
            "results": [
                {"assertion_id": assertion_id, "passed": passed}
                for assertion_id, passed in results
            ],
        }
    )
    return HarnessObservation(
        fixture_id=materialized.fixture_id,
        fixture_sha256=materialized.fixture_sha256,
        passed=not failing,
        assertions_total=len(results),
        assertions_passed=len(results) - len(failing),
        failing_assertion_ids=failing,
        evidence_sha256=evidence_sha,
    )


def _git(workspace: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=workspace,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def materialize_fixture(
    case: EvaluationCase,
    destination: Path,
    *,
    fixture_root: Path,
) -> tuple[MaterializedFixture, FixtureManifest]:
    """Create a root-commit repository containing only the seeded mutation."""
    fixture_dir = (fixture_root / case.fixture_repo).resolve()
    resolved_root = fixture_root.resolve()
    if resolved_root not in fixture_dir.parents:
        raise ValueError("fixture repository escaped the configured fixture root")
    manifest = load_fixture_manifest(fixture_dir)
    if manifest.fixture_id != case.case_id:
        raise ValueError("fixture id does not match evaluation case id")
    actual_digest = fixture_sha256(fixture_dir)
    if actual_digest != case.fixture_sha256:
        raise ValueError("fixture bytes do not match the corpus digest")

    shutil.copytree(fixture_dir / manifest.baseline_path, destination)
    baseline_tree_sha = _workspace_tree_sha256(destination)
    provisional = MaterializedFixture(
        fixture_id=manifest.fixture_id,
        fixture_sha256=actual_digest,
        workspace=destination,
        baseline_tree_sha256=baseline_tree_sha,
        mutation_commit_sha="0" * 40,
    )
    if not run_fixture_harness(provisional, manifest).passed:
        raise ValueError("fixture baseline does not satisfy its harness")

    patch_path = fixture_dir / manifest.mutation_patch
    _git(destination, "apply", "--check", str(patch_path))
    _git(destination, "apply", str(patch_path))
    if run_fixture_harness(provisional, manifest).passed:
        raise ValueError("synthetic mutation does not fail the fixture harness")

    _git(destination, "init", "--quiet", "--initial-branch=main")
    _git(destination, "config", "user.name", "APDL Evaluation")
    _git(destination, "config", "user.email", "evaluation@apdl.invalid")
    fixed_env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    _git(destination, "add", "--all")
    _git(
        destination,
        "commit",
        "--quiet",
        "-m",
        "evaluation workspace snapshot",
        env=fixed_env,
    )
    mutation_sha = _git(destination, "rev-parse", "HEAD")
    materialized = MaterializedFixture(
        fixture_id=manifest.fixture_id,
        fixture_sha256=actual_digest,
        workspace=destination,
        baseline_tree_sha256=baseline_tree_sha,
        mutation_commit_sha=mutation_sha,
    )
    return materialized, manifest
