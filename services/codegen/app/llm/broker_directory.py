"""Fail-closed filesystem authority for the Codegen LLM broker root."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


BROKER_DIRECTORY_MODE = 0o711
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | os.O_DIRECTORY
    | os.O_NOFOLLOW
    | getattr(os, "O_CLOEXEC", 0)
)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _assert_safe_parent(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("Codegen LLM broker parent must be a real directory")
    if metadata.st_uid not in {0, os.geteuid()}:
        raise RuntimeError(
            "Codegen LLM broker parent must be owned by root or this user"
        )
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022 and not mode & stat.S_ISVTX:
        raise RuntimeError(
            "Writable Codegen LLM broker parent must have the sticky bit"
        )


def _assert_suitable_root(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("Codegen LLM broker path must be a real directory")
    if metadata.st_uid != os.geteuid():
        raise RuntimeError(
            "Existing Codegen LLM broker directory must be owned by this user"
        )
    if stat.S_IMODE(metadata.st_mode) != BROKER_DIRECTORY_MODE:
        raise RuntimeError(
            "Existing Codegen LLM broker directory must already have mode 0711"
        )


def _validate_root_path(path: Path) -> None:
    if (
        not path.is_absolute()
        or path.name in {"", ".", ".."}
        or path.parent == path
    ):
        raise RuntimeError("Codegen LLM broker path must name one absolute directory")


@dataclass
class OpenBrokerRoot:
    """Pinned descriptor for one validated, non-symlink broker root."""

    path: Path
    resolved_path: Path
    descriptor: int

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def __enter__(self) -> OpenBrokerRoot:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def descriptor_path(self) -> Path:
        """Return an fd-backed path whose lookup cannot switch broker roots."""
        metadata = os.fstat(self.descriptor)
        for base in (Path("/proc/self/fd"),):
            candidate = base / str(self.descriptor)
            try:
                candidate_metadata = candidate.stat()
            except OSError:
                continue
            if _same_file(metadata, candidate_metadata):
                return candidate
        # macOS exposes directory descriptors under /dev/fd but does not allow
        # traversing through them. The resolved root lives in a validated
        # non-writable-or-sticky parent and is itself controller-owned 0711, so
        # an untrusted process cannot replace it. Reconfirm it is still the
        # descriptor-pinned inode immediately before using the stable path.
        try:
            resolved_metadata = self.resolved_path.stat(follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(
                "Codegen LLM broker root is no longer reachable"
            ) from exc
        if not _same_file(metadata, resolved_metadata):
            raise RuntimeError(
                "Codegen LLM broker root changed after validation"
            )
        return self.resolved_path


def open_broker_root(path: Path, *, create: bool) -> OpenBrokerRoot:
    """Open a validated root, optionally creating only its final component.

    The lexical parent may be a platform alias such as macOS ``/tmp``. Resolve
    that alias once, then pin the real parent and perform every root operation
    relative to its descriptor so a concurrent pathname swap cannot redirect
    creation or validation.
    """
    _validate_root_path(path)
    try:
        resolved_parent = path.parent.resolve(strict=True)
        parent_descriptor = os.open(resolved_parent, _DIRECTORY_FLAGS)
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise RuntimeError(
            "Codegen LLM broker parent directory must already exist"
        ) from exc
    try:
        _assert_safe_parent(os.fstat(parent_descriptor))
        created = False
        try:
            metadata = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if not create:
                raise RuntimeError(
                    "Codegen LLM broker directory must be prepared before serving"
                ) from None
            try:
                os.mkdir(path.name, mode=0o700, dir_fd=parent_descriptor)
                created = True
            except FileExistsError:
                pass
            metadata = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )

        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("Codegen LLM broker path must be a real directory")
        descriptor = os.open(
            path.name,
            _DIRECTORY_FLAGS,
            dir_fd=parent_descriptor,
        )
        try:
            opened_metadata = os.fstat(descriptor)
            if not _same_file(metadata, opened_metadata):
                raise RuntimeError(
                    "Codegen LLM broker directory changed while it was opened"
                )
            if created:
                if opened_metadata.st_uid != os.geteuid():
                    raise RuntimeError(
                        "Created Codegen LLM broker directory has the wrong owner"
                    )
                os.fchmod(descriptor, BROKER_DIRECTORY_MODE)
                opened_metadata = os.fstat(descriptor)
            _assert_suitable_root(opened_metadata)
        except BaseException:
            os.close(descriptor)
            raise
        return OpenBrokerRoot(
            path=path,
            resolved_path=resolved_parent / path.name,
            descriptor=descriptor,
        )
    finally:
        os.close(parent_descriptor)


def prepare_broker_root(path: Path) -> Path:
    """Create one missing dedicated root or validate an existing one."""
    with open_broker_root(path, create=True):
        return path
