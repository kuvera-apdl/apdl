"""Controller-owned reconstruction and publication of gated Codegen patches.

The model worker is deliberately not push-capable.  It returns a binary Git
patch plus the exact base and resulting tree identities.  This module rebuilds
that tree in a service-owned temporary repository, verifies the identity, and
uses a just-in-time write credential only for the final ``git push``.

No repository-defined command, hook, filter, or executable is run here.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.safety.secrets import MAX_SECRET_SCAN_BYTES, secret_kinds


_GIT_TIMEOUT_SECONDS = 300
_MAX_PATCH_BYTES = 16 * 1024 * 1024
_MAX_CHANGED_BLOB_BYTES = MAX_SECRET_SCAN_BYTES
_MAX_CHANGED_BLOBS_BYTES = 16 * 1024 * 1024
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_APDL_BRANCH_RE = re.compile(r"^apdl/[a-z0-9][a-z0-9._/-]{0,199}$")
_BASE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_REGULAR_BLOB_MODES = {b"100644", b"100755"}
_PERMITTED_BINARY_TYPES = (
    "application/pdf",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
)


class BranchPublicationError(RuntimeError):
    """The candidate tree could not be safely reconstructed or published."""


class UnsafeCandidateTreeError(BranchPublicationError):
    """The exact reconstructed candidate tree failed a permanent safety gate."""


@dataclass(frozen=True)
class PreparedBranch:
    """A verified controller-owned Git repository ready for one exact push."""

    repository: str
    branch: str
    base_sha: str
    expected_remote_sha: str | None
    candidate_head_sha: str
    head_sha: str
    tree_sha: str
    workspace: Path


@dataclass(frozen=True)
class PublishedBranch:
    """Exact remote branch identity observed after publication."""

    branch: str
    head_sha: str


@dataclass(frozen=True)
class _ChangedBlob:
    """One exact non-deleted blob from the reconstructed candidate index."""

    object_id: str
    path: bytes


class BranchPublisher(Protocol):
    """Controller seam used by initial generation and same-PR repair."""

    def prepare(
        self,
        *,
        repository: str,
        branch: str,
        base_branch: str,
        expected_base_sha: str,
        expected_remote_sha: str | None,
        candidate_head_sha: str,
        candidate_tree_sha: str,
        patch_base64: str,
        commit_title: str,
        read_token: str,
    ) -> contextlib.AbstractAsyncContextManager[PreparedBranch]: ...

    async def push(
        self,
        prepared: PreparedBranch,
        *,
        write_token: str,
    ) -> PublishedBranch: ...

    async def recover_published(
        self,
        *,
        repository: str,
        branch: str,
        candidate_tree_sha: str,
        read_token: str,
    ) -> PublishedBranch | None: ...


def _validate_repository(value: str) -> str:
    if not _REPOSITORY_RE.fullmatch(value):
        raise BranchPublicationError("repository must be an owner/name identifier")
    return value


def _validate_branch(value: str) -> str:
    if (
        not _APDL_BRANCH_RE.fullmatch(value)
        or ".." in value
        or "//" in value
        or "@{" in value
        or value.endswith(("/", ".", ".lock"))
    ):
        raise BranchPublicationError("branch is not a canonical APDL branch")
    return value


def _validate_base_branch(value: str) -> str:
    if (
        not _BASE_BRANCH_RE.fullmatch(value)
        or value.endswith("/")
        or ".." in value
        or "//" in value
        or "@{" in value
        or value.endswith((".", ".lock"))
    ):
        raise BranchPublicationError("base branch is not a safe Git ref component")
    return value


def _validate_sha(value: str, label: str) -> str:
    if not _SHA_RE.fullmatch(value):
        raise BranchPublicationError(f"{label} must be an exact Git object id")
    return value


def _decode_patch(value: str) -> bytes:
    try:
        encoded = value.encode("ascii", "strict")
        patch = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise BranchPublicationError("candidate patch is not canonical base64") from exc
    if not patch:
        raise BranchPublicationError("candidate patch must not be empty")
    if len(patch) > _MAX_PATCH_BYTES:
        raise BranchPublicationError(
            f"candidate patch exceeds {_MAX_PATCH_BYTES} bytes"
        )
    return patch


def _commit_subject(title: str) -> str:
    normalized = " ".join(title.split())
    if not normalized:
        normalized = "Apply approved Codegen change"
    return normalized[:200]


def _repository_url(repository: str) -> str:
    return f"https://github.com/{repository}.git"


def _git_environment(token: str | None = None) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": tempfile.gettempdir(),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if token is not None:
        raw = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {raw}",
            }
        )
    return environment


async def _run_git(
    workspace: Path | None,
    args: list[str],
    *,
    token: str | None = None,
    input_bytes: bytes | None = None,
) -> bytes:
    argv = ["git"]
    if workspace is not None:
        argv.extend(["-C", workspace.as_posix()])
    argv.extend(args)
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=_git_environment(token),
        stdin=(asyncio.subprocess.PIPE if input_bytes is not None else None),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(input_bytes),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise BranchPublicationError(
            f"controller Git command timed out: {' '.join(args[:2])}"
        ) from exc
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    output = stdout or b""
    if process.returncode != 0:
        detail = output[-4000:].decode("utf-8", "replace")
        raise BranchPublicationError(
            f"controller Git command failed: {' '.join(args[:2])}: {detail}"
        )
    return output


def _assert_no_symlink_entries(index: bytes) -> None:
    if not index:
        return
    if not index.endswith(b"\x00"):
        raise BranchPublicationError("Git index inventory is not NUL-terminated")
    for record in index[:-1].split(b"\x00"):
        metadata, separator, _path = record.partition(b"\t")
        if not separator:
            raise BranchPublicationError("Git index inventory is malformed")
        mode = metadata.split(b" ", 1)[0]
        if mode == b"120000":
            raise BranchPublicationError(
                "candidate tree contains a symbolic link and cannot be published"
            )


def _parse_changed_blob_inventory(inventory: bytes) -> tuple[_ChangedBlob, ...]:
    """Parse `git diff-index --raw -z` without decoding attacker-owned paths."""
    if not inventory:
        return ()
    if not inventory.endswith(b"\x00"):
        raise UnsafeCandidateTreeError(
            "candidate changed-blob inventory is not NUL-terminated"
        )
    records = inventory[:-1].split(b"\x00")
    if len(records) % 2:
        raise UnsafeCandidateTreeError(
            "candidate changed-blob inventory is malformed"
        )

    changed: list[_ChangedBlob] = []
    for header, path in zip(records[::2], records[1::2], strict=True):
        if not path or not header.startswith(b":"):
            raise UnsafeCandidateTreeError(
                "candidate changed-blob inventory is malformed"
            )
        fields = header[1:].split(b" ")
        if len(fields) != 5:
            raise UnsafeCandidateTreeError(
                "candidate changed-blob inventory is malformed"
            )
        old_mode, new_mode, old_object, new_object, status = fields
        if (
            re.fullmatch(rb"[0-7]{6}", old_mode) is None
            or re.fullmatch(rb"[0-7]{6}", new_mode) is None
            or status not in {b"A", b"D", b"M", b"T"}
        ):
            raise UnsafeCandidateTreeError(
                "candidate changed-blob inventory is malformed"
            )
        try:
            old_object_id = old_object.decode("ascii", "strict")
            new_object_id = new_object.decode("ascii", "strict")
        except UnicodeDecodeError as exc:
            raise UnsafeCandidateTreeError(
                "candidate changed-blob inventory is malformed"
            ) from exc
        if (
            not _SHA_RE.fullmatch(old_object_id)
            or not _SHA_RE.fullmatch(new_object_id)
        ):
            raise UnsafeCandidateTreeError(
                "candidate changed-blob inventory contains an invalid object id"
            )

        if status == b"D":
            if new_mode != b"000000" or set(new_object) != {ord("0")}:
                raise UnsafeCandidateTreeError(
                    "candidate deletion inventory is malformed"
                )
            continue
        if new_mode not in _REGULAR_BLOB_MODES:
            raise UnsafeCandidateTreeError(
                "candidate changed entry is not a regular file blob"
            )
        if set(new_object) == {ord("0")}:
            raise UnsafeCandidateTreeError(
                "candidate changed blob has a missing object id"
            )
        changed.append(_ChangedBlob(object_id=new_object_id, path=path))
    return tuple(changed)


def _is_text_blob(content: bytes) -> bool:
    """Accept only strict UTF-8 text without binary control characters."""
    if b"\x00" in content:
        return False
    try:
        text = content.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return False
    return all(
        character in "\t\n\r\f" or ord(character) >= 0x20 for character in text
    )


def _permitted_binary_type(path: bytes, content: bytes) -> str | None:
    """Resolve a narrow binary allowlist from both suffix and file magic."""
    normalized_path = path.lower()
    if normalized_path.endswith(b".png") and content.startswith(
        b"\x89PNG\r\n\x1a\n"
    ):
        return "image/png"
    if normalized_path.endswith((b".jpg", b".jpeg")) and content.startswith(
        b"\xff\xd8\xff"
    ):
        return "image/jpeg"
    if normalized_path.endswith(b".gif") and content.startswith(
        (b"GIF87a", b"GIF89a")
    ):
        return "image/gif"
    if (
        normalized_path.endswith(b".webp")
        and len(content) >= 12
        and content.startswith(b"RIFF")
        and content[8:12] == b"WEBP"
    ):
        return "image/webp"
    if normalized_path.endswith(b".pdf") and content.startswith(b"%PDF-"):
        return "application/pdf"
    return None


async def _scan_changed_blobs(workspace: Path, base_sha: str) -> None:
    """Fail closed unless every exact changed blob is bounded and secret-free."""
    inventory = await _run_git(
        workspace,
        [
            "diff-index",
            "--cached",
            "--raw",
            "-z",
            "--no-renames",
            "--abbrev=64",
            base_sha,
            "--",
        ],
    )
    changed = _parse_changed_blob_inventory(inventory)
    aggregate_bytes = 0
    for blob in changed:
        object_type = (
            (await _run_git(workspace, ["cat-file", "-t", blob.object_id]))
            .decode("ascii", "strict")
            .strip()
        )
        if object_type != "blob":
            raise UnsafeCandidateTreeError(
                "candidate changed entry does not resolve to a Git blob"
            )
        size_text = (
            (await _run_git(workspace, ["cat-file", "-s", blob.object_id]))
            .decode("ascii", "strict")
            .strip()
        )
        if re.fullmatch(r"(?:0|[1-9][0-9]*)", size_text) is None:
            raise UnsafeCandidateTreeError(
                "candidate changed blob has a malformed size"
            )
        blob_bytes = int(size_text)
        if blob_bytes > _MAX_CHANGED_BLOB_BYTES:
            raise UnsafeCandidateTreeError(
                "candidate changed blob exceeds the "
                f"{_MAX_CHANGED_BLOB_BYTES}-byte safety limit"
            )
        aggregate_bytes += blob_bytes
        if aggregate_bytes > _MAX_CHANGED_BLOBS_BYTES:
            raise UnsafeCandidateTreeError(
                "candidate changed blobs exceed the "
                f"{_MAX_CHANGED_BLOBS_BYTES}-byte aggregate safety limit"
            )

        content = await _run_git(workspace, ["cat-file", "blob", blob.object_id])
        if len(content) != blob_bytes:
            raise UnsafeCandidateTreeError(
                "candidate changed blob content is incomplete"
            )
        if not _is_text_blob(content):
            media_type = _permitted_binary_type(blob.path, content)
            if media_type is None:
                permitted = ", ".join(_PERMITTED_BINARY_TYPES)
                raise UnsafeCandidateTreeError(
                    "candidate changed blob has an unsupported binary type; "
                    f"permitted types: {permitted}"
                )
        kinds = secret_kinds(content, max_bytes=_MAX_CHANGED_BLOB_BYTES)
        if kinds:
            descriptions = ", ".join(kind.replace("_", " ") for kind in kinds)
            raise UnsafeCandidateTreeError(
                "candidate changed blob contains possible "
                f"{descriptions} secret material"
            )


class GitBranchPublisher:
    """Reconstruct a candidate with read authority, then push with JIT write."""

    @asynccontextmanager
    async def prepare(
        self,
        *,
        repository: str,
        branch: str,
        base_branch: str,
        expected_base_sha: str,
        expected_remote_sha: str | None,
        candidate_head_sha: str,
        candidate_tree_sha: str,
        patch_base64: str,
        commit_title: str,
        read_token: str,
    ) -> AsyncIterator[PreparedBranch]:
        repository = _validate_repository(repository)
        branch = _validate_branch(branch)
        base_branch = _validate_base_branch(base_branch)
        expected_base_sha = _validate_sha(expected_base_sha, "candidate base SHA")
        candidate_head_sha = _validate_sha(candidate_head_sha, "candidate head SHA")
        candidate_tree_sha = _validate_sha(candidate_tree_sha, "candidate tree SHA")
        if expected_remote_sha is not None:
            expected_remote_sha = _validate_sha(
                expected_remote_sha, "expected remote SHA"
            )
            if expected_remote_sha != expected_base_sha:
                raise BranchPublicationError(
                    "repair base SHA must equal its expected remote lease"
                )
        patch = _decode_patch(patch_base64)
        workspace = Path(tempfile.mkdtemp(prefix="apdl-publish-"))
        try:
            await _run_git(None, ["init", "--quiet", workspace.as_posix()])
            await _run_git(workspace, ["config", "core.symlinks", "false"])
            await _run_git(workspace, ["config", "core.hooksPath", os.devnull])
            await _run_git(workspace, ["config", "user.email", "codegen@apdl.dev"])
            await _run_git(workspace, ["config", "user.name", "APDL Codegen"])
            await _run_git(
                workspace,
                [
                    "remote",
                    "add",
                    "origin",
                    _repository_url(repository),
                ],
            )
            source_branch = branch if expected_remote_sha is not None else base_branch
            await _run_git(
                workspace,
                [
                    "fetch",
                    "--depth",
                    "1",
                    "origin",
                    f"refs/heads/{source_branch}",
                ],
                token=read_token,
            )
            observed_base = (
                (await _run_git(workspace, ["rev-parse", "FETCH_HEAD"]))
                .decode("ascii", "strict")
                .strip()
            )
            if observed_base != expected_base_sha:
                raise BranchPublicationError(
                    "repository head changed after candidate generation"
                )
            await _run_git(
                workspace,
                ["checkout", "--quiet", "--detach", observed_base],
            )
            await _run_git(
                workspace,
                ["checkout", "--quiet", "-B", branch],
            )
            await _run_git(
                workspace,
                [
                    "apply",
                    "--index",
                    "--binary",
                    "--whitespace=nowarn",
                    "-",
                ],
                input_bytes=patch,
            )
            index = await _run_git(workspace, ["ls-files", "-s", "-z"])
            _assert_no_symlink_entries(index)
            observed_tree = (
                (await _run_git(workspace, ["write-tree"]))
                .decode("ascii", "strict")
                .strip()
            )
            if observed_tree != candidate_tree_sha:
                raise BranchPublicationError(
                    "controller-reconstructed tree does not match the gated candidate"
                )
            # This is the authoritative content boundary. The index's exact tree
            # identity is already proven, while the caller cannot mint/use its
            # write token until this context manager yields.
            await _scan_changed_blobs(workspace, expected_base_sha)
            await _run_git(
                workspace,
                [
                    "commit",
                    "--quiet",
                    "--no-verify",
                    "-m",
                    _commit_subject(commit_title),
                ],
            )
            head_sha = (
                (await _run_git(workspace, ["rev-parse", "HEAD"]))
                .decode("ascii", "strict")
                .strip()
            )
            _validate_sha(head_sha, "controller commit SHA")
            yield PreparedBranch(
                repository=repository,
                branch=branch,
                base_sha=expected_base_sha,
                expected_remote_sha=expected_remote_sha,
                candidate_head_sha=candidate_head_sha,
                head_sha=head_sha,
                tree_sha=observed_tree,
                workspace=workspace,
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    async def push(
        self,
        prepared: PreparedBranch,
        *,
        write_token: str,
    ) -> PublishedBranch:
        lease = (
            prepared.expected_remote_sha
            if prepared.expected_remote_sha is not None
            else ""
        )
        await _run_git(
            prepared.workspace,
            [
                "push",
                f"--force-with-lease=refs/heads/{prepared.branch}:{lease}",
                "origin",
                f"HEAD:refs/heads/{prepared.branch}",
            ],
            token=write_token,
        )
        remote = await _run_git(
            prepared.workspace,
            ["ls-remote", "origin", f"refs/heads/{prepared.branch}"],
            token=write_token,
        )
        line = remote.decode("utf-8", "strict").strip()
        values = line.split("\t")
        if len(values) != 2 or values[1] != f"refs/heads/{prepared.branch}":
            raise BranchPublicationError(
                "published branch could not be resolved exactly"
            )
        head_sha = _validate_sha(values[0], "published branch SHA")
        if head_sha != prepared.head_sha:
            raise BranchPublicationError(
                "published branch SHA does not match the controller commit"
            )
        return PublishedBranch(branch=prepared.branch, head_sha=head_sha)

    async def recover_published(
        self,
        *,
        repository: str,
        branch: str,
        candidate_tree_sha: str,
        read_token: str,
    ) -> PublishedBranch | None:
        """Observe an existing APDL branch only when its exact tree is gated."""
        repository = _validate_repository(repository)
        branch = _validate_branch(branch)
        candidate_tree_sha = _validate_sha(candidate_tree_sha, "candidate tree SHA")
        ref = f"refs/heads/{branch}"
        remote = await _run_git(
            None,
            ["ls-remote", _repository_url(repository), ref],
            token=read_token,
        )
        line = remote.decode("utf-8", "strict").strip()
        if not line:
            return None
        values = line.split("\t")
        if len(values) != 2 or values[1] != ref:
            raise BranchPublicationError(
                "existing APDL branch could not be resolved exactly"
            )
        head_sha = _validate_sha(values[0], "existing APDL branch SHA")
        workspace = Path(tempfile.mkdtemp(prefix="apdl-recover-publish-"))
        try:
            await _run_git(None, ["init", "--quiet", workspace.as_posix()])
            await _run_git(
                workspace,
                ["remote", "add", "origin", _repository_url(repository)],
            )
            await _run_git(
                workspace,
                ["fetch", "--depth", "1", "origin", ref],
                token=read_token,
            )
            observed_head = (
                (await _run_git(workspace, ["rev-parse", "FETCH_HEAD"]))
                .decode("ascii", "strict")
                .strip()
            )
            if observed_head != head_sha:
                raise BranchPublicationError(
                    "existing APDL branch changed during recovery"
                )
            observed_tree = (
                (await _run_git(workspace, ["rev-parse", "FETCH_HEAD^{tree}"]))
                .decode("ascii", "strict")
                .strip()
            )
            if observed_tree != candidate_tree_sha:
                raise BranchPublicationError(
                    "existing APDL branch tree differs from the gated candidate"
                )
            return PublishedBranch(branch=branch, head_sha=head_sha)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
