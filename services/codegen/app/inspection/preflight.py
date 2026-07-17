"""Credential-free repository preflight and exact-tree attestation.

The production controller runs this inspection in a dedicated ephemeral
container before it launches the model-bearing editor container.  The
attestation contains no repository text: it proves only that one exact Git tree
was exhaustively inventoried through :class:`RepositoryInspector` and contained
no symlink or other unsafe filesystem entry.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from app.inspection.models import StrictModel
from app.inspection.repository import InspectionPathError, RepositoryInspector

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SAFE_INDEX_MODES = frozenset({"100644", "100755"})


class RepositoryPreflightAttestation(StrictModel):
    """Exact repository tree approved by the credential-free inspector."""

    schema_version: Literal["repository_preflight@1"] = "repository_preflight@1"
    repository: str
    source_branch: str
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    file_count: int = Field(ge=0)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if not _REPOSITORY.fullmatch(value):
            raise ValueError("repository must use the canonical owner/name form")
        return value

    @field_validator("source_branch")
    @classmethod
    def validate_source_branch(cls, value: str) -> str:
        if (
            not _BRANCH.fullmatch(value)
            or value.endswith(("/", "."))
            or ".." in value
            or "@{" in value
            or value.startswith("-")
        ):
            raise ValueError("source_branch is not a canonical Git branch name")
        return value


def _git(repo_dir: Path, *args: str, text: bool = True) -> str | bytes:
    """Run a bounded, credential-free local Git inspection command."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            check=False,
            capture_output=True,
            text=text,
            timeout=30,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InspectionPathError("repository Git identity inspection failed") from exc
    if completed.returncode != 0:
        raise InspectionPathError("repository Git identity inspection failed")
    return completed.stdout


def _assert_regular_index(repo_dir: Path) -> None:
    """Reject symlinks, submodules, conflict stages, and malformed index paths."""
    raw = _git(repo_dir, "ls-files", "--stage", "-z", text=False)
    assert isinstance(raw, bytes)
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_sha, stage = metadata.decode("ascii").split(" ")
            raw_path.decode("utf-8", "strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise InspectionPathError(
                "repository index contains a malformed path entry"
            ) from exc
        if (
            mode not in _SAFE_INDEX_MODES
            or stage != "0"
            or not _GIT_SHA.fullmatch(object_sha)
        ):
            raise InspectionPathError(
                "repository index contains a symlink or non-regular entry"
            )


def attest_repository_checkout(
    repo_dir: Path,
    *,
    repository: str,
    source_branch: str,
) -> RepositoryPreflightAttestation:
    """Exhaustively inspect and bind one clean checkout to its Git identities."""
    root = Path(repo_dir)
    _assert_regular_index(root)
    inventory = RepositoryInspector(
        root,
        max_files=50_000,
        max_inventory_entries=100_000,
    ).inventory()
    if inventory.truncated:
        raise InspectionPathError(
            "repository is too large for exhaustive safety inspection"
        )
    head_sha = str(_git(root, "rev-parse", "HEAD")).strip()
    tree_sha = str(_git(root, "rev-parse", "HEAD^{tree}")).strip()
    if not _GIT_SHA.fullmatch(head_sha) or not _GIT_SHA.fullmatch(tree_sha):
        raise InspectionPathError("repository returned a malformed Git identity")
    status = str(
        _git(root, "status", "--porcelain", "--untracked-files=all")
    ).strip()
    if status:
        raise InspectionPathError("repository preflight requires a clean checkout")
    return RepositoryPreflightAttestation(
        repository=repository,
        source_branch=source_branch,
        head_sha=head_sha,
        tree_sha=tree_sha,
        file_count=len(inventory.paths),
    )
