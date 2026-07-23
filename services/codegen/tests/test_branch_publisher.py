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


def _candidate(
    tmp_path: Path,
    *,
    symlink: bool = False,
    changed_files: dict[str, bytes] | None = None,
) -> dict[str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
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

    if symlink and changed_files is not None:
        raise ValueError("symlink and changed_files are mutually exclusive")
    if symlink:
        os.symlink("/etc/passwd", source / "outside")
        _git(source, "add", "outside")
    elif changed_files is not None:
        for relative_path, content in changed_files.items():
            target = source / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        _git(source, "add", "--", *changed_files)
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
async def test_recovery_accepts_only_an_existing_branch_with_the_gated_tree(
    monkeypatch, tmp_path
):
    candidate = _candidate(tmp_path)
    _git(
        Path(candidate["source"]),
        "push",
        candidate["remote"],
        f"{candidate['head_sha']}:refs/heads/apdl/change-cs_123",
    )
    monkeypatch.setattr(
        publisher_module,
        "_repository_url",
        lambda _repository: candidate["remote"],
    )

    recovered = await GitBranchPublisher().recover_published(
        repository="owner/repo",
        branch="apdl/change-cs_123",
        candidate_tree_sha=candidate["tree_sha"],
        read_token="read-token",
    )

    assert recovered is not None
    assert recovered.head_sha == candidate["head_sha"]

    with pytest.raises(BranchPublicationError, match="differs"):
        await GitBranchPublisher().recover_published(
            repository="owner/repo",
            branch="apdl/change-cs_123",
            candidate_tree_sha="d" * 40,
            read_token="read-token",
        )


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


@pytest.mark.asyncio
async def test_prepare_rejects_secret_embedded_in_permitted_binary_blob(
    monkeypatch,
    tmp_path,
):
    token = b"ghp_" + (b"A" * 32)
    candidate = _candidate(
        tmp_path,
        changed_files={
            "assets/payload\nwith-tab\t.png": (
                b"\x89PNG\r\n\x1a\n"
                b"\x00binary-metadata:"
                + token
                + b"\x00"
            )
        },
    )
    monkeypatch.setattr(
        publisher_module,
        "_repository_url",
        lambda _repository: candidate["remote"],
    )

    with pytest.raises(
        BranchPublicationError,
        match="possible github token secret material",
    ):
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
            pytest.fail("secret-bearing binary candidate unexpectedly prepared")

    remote = Path(candidate["remote"])
    unpublished = subprocess.run(
        [
            "git",
            "-C",
            remote.as_posix(),
            "show-ref",
            "--verify",
            "refs/heads/apdl/change-cs_123",
        ],
        check=False,
        capture_output=True,
    )
    assert unpublished.returncode != 0


@pytest.mark.asyncio
async def test_prepare_accepts_bounded_secret_free_permitted_binary(
    monkeypatch,
    tmp_path,
):
    candidate = _candidate(
        tmp_path,
        changed_files={
            "assets/icon.png": (
                b"\x89PNG\r\n\x1a\n\x00bounded-secret-free-image-data\x00"
            )
        },
    )
    monkeypatch.setattr(
        publisher_module,
        "_repository_url",
        lambda _repository: candidate["remote"],
    )

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
    ) as prepared:
        assert prepared.tree_sha == candidate["tree_sha"]


@pytest.mark.asyncio
async def test_prepare_rejects_unsupported_binary_type(monkeypatch, tmp_path):
    candidate = _candidate(
        tmp_path,
        changed_files={"assets/payload.bin": b"\x00unclassified-binary\xff"},
    )
    monkeypatch.setattr(
        publisher_module,
        "_repository_url",
        lambda _repository: candidate["remote"],
    )

    with pytest.raises(BranchPublicationError, match="unsupported binary type"):
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
            pytest.fail("unsupported binary candidate unexpectedly prepared")


@pytest.mark.asyncio
async def test_prepare_enforces_per_blob_and_aggregate_scan_limits(
    monkeypatch,
    tmp_path,
):
    per_blob_candidate = _candidate(
        tmp_path / "per-blob",
        changed_files={"large.txt": b"x" * 17},
    )
    monkeypatch.setattr(
        publisher_module,
        "_repository_url",
        lambda _repository: per_blob_candidate["remote"],
    )
    monkeypatch.setattr(publisher_module, "_MAX_CHANGED_BLOB_BYTES", 16)

    with pytest.raises(BranchPublicationError, match="16-byte safety limit"):
        async with GitBranchPublisher().prepare(
            repository="owner/repo",
            branch="apdl/change-cs_123",
            base_branch="main",
            expected_base_sha=per_blob_candidate["base_sha"],
            expected_remote_sha=None,
            candidate_head_sha=per_blob_candidate["head_sha"],
            candidate_tree_sha=per_blob_candidate["tree_sha"],
            patch_base64=per_blob_candidate["patch_base64"],
            commit_title="Apply the candidate",
            read_token="read-token",
        ):
            pytest.fail("oversized candidate blob unexpectedly prepared")

    aggregate_candidate = _candidate(
        tmp_path / "aggregate",
        changed_files={"first.txt": b"a" * 12, "second.txt": b"b" * 12},
    )
    monkeypatch.setattr(
        publisher_module,
        "_repository_url",
        lambda _repository: aggregate_candidate["remote"],
    )
    monkeypatch.setattr(publisher_module, "_MAX_CHANGED_BLOB_BYTES", 16)
    monkeypatch.setattr(publisher_module, "_MAX_CHANGED_BLOBS_BYTES", 20)

    with pytest.raises(
        BranchPublicationError,
        match="20-byte aggregate safety limit",
    ):
        async with GitBranchPublisher().prepare(
            repository="owner/repo",
            branch="apdl/change-cs_123",
            base_branch="main",
            expected_base_sha=aggregate_candidate["base_sha"],
            expected_remote_sha=None,
            candidate_head_sha=aggregate_candidate["head_sha"],
            candidate_tree_sha=aggregate_candidate["tree_sha"],
            patch_base64=aggregate_candidate["patch_base64"],
            commit_title="Apply the candidate",
            read_token="read-token",
        ):
            pytest.fail("aggregate-oversized candidate unexpectedly prepared")


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
