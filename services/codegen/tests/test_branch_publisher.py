"""Controller-owned Git reconstruction and publication tests."""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
from pathlib import Path

import pytest

import app.github.publisher as publisher_module
from app.github.publisher import (
    BranchPublicationError,
    GitBranchPublisher,
    _git_environment,
    _run_git,
)


def _git(path: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", path.as_posix(), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout


def _candidate(tmp_path: Path, *, symlink: bool = False) -> dict[str, str]:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", source.as_posix()],
        check=True,
    )
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    (source / "README.md").write_text("base\n")
    _git(source, "add", "README.md")
    _git(source, "commit", "--quiet", "-m", "base")
    base_sha = _git(source, "rev-parse", "HEAD").decode().strip()
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", source.as_posix(), remote.as_posix()],
        check=True,
    )

    if symlink:
        os.symlink("/etc/passwd", source / "outside")
        _git(source, "add", "outside")
    else:
        (source / "README.md").write_text("base\ncandidate\n")
        _git(source, "add", "README.md")
    _git(source, "commit", "--quiet", "-m", "candidate")
    head_sha = _git(source, "rev-parse", "HEAD").decode().strip()
    tree_sha = _git(source, "rev-parse", "HEAD^{tree}").decode().strip()
    patch = _git(
        source,
        "diff",
        "--binary",
        "--full-index",
        "--no-renames",
        base_sha,
        head_sha,
    )
    return {
        "source": source.as_posix(),
        "remote": remote.as_posix(),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "tree_sha": tree_sha,
        "patch_base64": base64.b64encode(patch).decode("ascii"),
    }


@pytest.mark.asyncio
async def test_reconstructs_exact_tree_and_pushes_with_an_empty_branch_lease(
    monkeypatch, tmp_path
):
    candidate = _candidate(tmp_path)
    monkeypatch.setattr(
        publisher_module,
        "_repository_url",
        lambda _repository: candidate["remote"],
    )

    branch_publisher = GitBranchPublisher()
    async with branch_publisher.prepare(
        repository="owner/repo",
        branch="apdl/change-cs_123",
        base_branch="main",
        expected_base_sha=candidate["base_sha"],
        expected_remote_sha=None,
        candidate_head_sha=candidate["head_sha"],
        candidate_tree_sha=candidate["tree_sha"],
        patch_base64=candidate["patch_base64"],
        commit_title="Apply the candidate",
        read_token="read-token",
    ) as prepared:
        assert prepared.tree_sha == candidate["tree_sha"]
        assert prepared.candidate_head_sha == candidate["head_sha"]
        published = await branch_publisher.push(
            prepared,
            write_token="write-token",
        )

    remote = Path(candidate["remote"])
    observed_head = (
        _git(remote, "rev-parse", "refs/heads/apdl/change-cs_123").decode().strip()
    )
    observed_tree = (
        _git(remote, "rev-parse", f"{observed_head}^{{tree}}").decode().strip()
    )
    assert published.head_sha == observed_head
    assert observed_tree == candidate["tree_sha"]


@pytest.mark.asyncio
async def test_repair_push_requires_the_exact_existing_remote_head(
    monkeypatch, tmp_path
):
    candidate = _candidate(tmp_path)
    remote = Path(candidate["remote"])
    _git(
        remote,
        "update-ref",
        "refs/heads/apdl/change-cs_123",
        candidate["base_sha"],
    )
    monkeypatch.setattr(
        publisher_module,
        "_repository_url",
        lambda _repository: candidate["remote"],
    )

    branch_publisher = GitBranchPublisher()
    async with branch_publisher.prepare(
        repository="owner/repo",
        branch="apdl/change-cs_123",
        base_branch="main",
        expected_base_sha=candidate["base_sha"],
        expected_remote_sha=candidate["base_sha"],
        candidate_head_sha=candidate["head_sha"],
        candidate_tree_sha=candidate["tree_sha"],
        patch_base64=candidate["patch_base64"],
        commit_title="Repair the candidate",
        read_token="read-token",
    ) as prepared:
        _git(
            Path(candidate["source"]),
            "push",
            candidate["remote"],
            f"{candidate['head_sha']}:refs/heads/competing",
        )
        _git(
            remote,
            "update-ref",
            "refs/heads/apdl/change-cs_123",
            candidate["head_sha"],
            candidate["base_sha"],
        )
        with pytest.raises(BranchPublicationError, match="Git command failed"):
            await branch_publisher.push(prepared, write_token="write-token")


@pytest.mark.asyncio
async def test_prepare_rejects_a_stale_base_before_applying_the_patch(
    monkeypatch, tmp_path
):
    candidate = _candidate(tmp_path)
    monkeypatch.setattr(
        publisher_module,
        "_repository_url",
        lambda _repository: candidate["remote"],
    )

    with pytest.raises(BranchPublicationError, match="head changed"):
        async with GitBranchPublisher().prepare(
            repository="owner/repo",
            branch="apdl/change-cs_123",
            base_branch="main",
            expected_base_sha="d" * 40,
            expected_remote_sha=None,
            candidate_head_sha=candidate["head_sha"],
            candidate_tree_sha=candidate["tree_sha"],
            patch_base64=candidate["patch_base64"],
            commit_title="Apply the candidate",
            read_token="read-token",
        ):
            pytest.fail("stale candidate unexpectedly prepared")


@pytest.mark.asyncio
async def test_prepare_rejects_candidate_symlinks(monkeypatch, tmp_path):
    candidate = _candidate(tmp_path, symlink=True)
    monkeypatch.setattr(
        publisher_module,
        "_repository_url",
        lambda _repository: candidate["remote"],
    )

    with pytest.raises(BranchPublicationError, match="symbolic link"):
        async with GitBranchPublisher().prepare(
            repository="owner/repo",
            branch="apdl/change-cs_123",
            base_branch="main",
            expected_base_sha=candidate["base_sha"],
            expected_remote_sha=None,
            candidate_head_sha=candidate["head_sha"],
            candidate_tree_sha=candidate["tree_sha"],
            patch_base64=candidate["patch_base64"],
            commit_title="Apply the candidate",
            read_token="read-token",
        ):
            pytest.fail("symlink candidate unexpectedly prepared")


def test_git_credentials_are_encoded_only_in_the_subprocess_environment():
    environment = _git_environment("plain-secret-token")

    assert "plain-secret-token" not in "\n".join(environment.values())
    assert environment["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.asyncio
async def test_run_git_never_places_the_token_in_argv(monkeypatch):
    observed: dict[str, object] = {}

    class _Process:
        returncode = 0

        async def communicate(self, _input):
            return b"git version", b""

    async def create_subprocess_exec(*args, **kwargs):
        observed["args"] = args
        observed["env"] = kwargs["env"]
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)

    await _run_git(None, ["version"], token="plain-secret-token")

    assert "plain-secret-token" not in "\n".join(observed["args"])
    assert "plain-secret-token" not in "\n".join(observed["env"].values())
