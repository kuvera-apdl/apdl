"""Focused tests for credential-free exact-tree repository preflight."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.inspection.preflight import (
    RepositoryPreflightAttestation,
    attest_repository_checkout,
)
from app.inspection.repository import InspectionPathError
from app.inspection import preflight_cli


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("safe repository\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_attestation_binds_exhaustive_clean_tree(tmp_path: Path):
    repo = _repository(tmp_path)

    attestation = attest_repository_checkout(
        repo,
        repository="acme/widgets",
        source_branch="main",
    )

    assert attestation.repository == "acme/widgets"
    assert attestation.source_branch == "main"
    assert attestation.head_sha == _git(repo, "rev-parse", "HEAD")
    assert attestation.tree_sha == _git(repo, "rev-parse", "HEAD^{tree}")
    assert attestation.file_count == 1


def test_attestation_rejects_committed_symlink_before_reading_it(tmp_path: Path):
    repo = _repository(tmp_path)
    outside = tmp_path / "provider.env"
    outside.write_text("OPENAI_API_KEY=provider-secret\n", encoding="utf-8")
    (repo / "README.md").unlink()
    (repo / "README.md").symlink_to(outside)
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "replace with symlink")

    with pytest.raises(InspectionPathError, match="symlink or non-regular"):
        attest_repository_checkout(
            repo,
            repository="acme/widgets",
            source_branch="main",
        )


def test_attestation_rejects_dirty_or_untracked_checkout(tmp_path: Path):
    repo = _repository(tmp_path)
    (repo / "untracked.txt").write_text("not attested\n", encoding="utf-8")

    with pytest.raises(InspectionPathError, match="clean checkout"):
        attest_repository_checkout(
            repo,
            repository="acme/widgets",
            source_branch="main",
        )


def test_attestation_schema_is_strict():
    with pytest.raises(ValidationError):
        RepositoryPreflightAttestation.model_validate(
            {
                "schema_version": "repository_preflight@1",
                "repository": "acme/widgets",
                "source_branch": "main",
                "head_sha": "a" * 40,
                "tree_sha": "b" * 40,
                "file_count": 1,
                "legacy_head": "c" * 40,
            }
        )


def test_cli_keeps_clone_stderr_and_exception_text_inside_boundary(
    monkeypatch,
    capsys,
    tmp_path,
):
    monkeypatch.setenv("CS_REPO", "acme/widgets")
    monkeypatch.setenv("CS_SOURCE_BRANCH", "main")
    monkeypatch.setenv("CODEGEN_WORKDIR", str(tmp_path))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"read_token": "ghs_read"}) + "\n"),
    )

    def fail_clone(**_kwargs):
        raise RuntimeError("provider-secret from raw git stderr")

    monkeypatch.setattr(preflight_cli, "_clone", fail_clone)

    assert preflight_cli.main() == 1
    output = capsys.readouterr().out
    assert "provider-secret" not in output
    assert "raw git stderr" not in output
    assert json.loads(output)["error"] == (
        "repository preflight refused: RuntimeError"
    )
