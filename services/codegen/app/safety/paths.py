"""Canonical changed-path parsing for security-sensitive Git diff consumers."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Sequence


class ChangedPathError(ValueError):
    """Raised when Git path output is ambiguous, malformed, or non-canonical."""


def canonical_changed_path(path: str) -> str:
    """Validate one exact repository-relative POSIX path without rewriting it."""
    if not isinstance(path, str):
        raise ChangedPathError("changed paths must be strings")
    if not path or "\x00" in path:
        raise ChangedPathError("changed paths must be non-empty and NUL-free")
    try:
        path.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ChangedPathError("changed paths must contain valid UTF-8 text") from exc
    pure = PurePosixPath(path)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ChangedPathError(f"changed path must stay repository-relative: {path!r}")
    canonical = pure.as_posix()
    if canonical != path:
        raise ChangedPathError(
            f"changed path must use canonical repository-relative form: {path!r}"
        )
    return canonical


def canonical_changed_paths(paths: Sequence[str]) -> list[str]:
    """Validate an exact changed-path collection and reject duplicate entries."""
    if isinstance(paths, (str, bytes, bytearray)) or not isinstance(paths, Sequence):
        raise ChangedPathError("changed paths must be a sequence of strings")
    canonical = [canonical_changed_path(path) for path in paths]
    if len(canonical) != len(set(canonical)):
        raise ChangedPathError("changed paths must not contain duplicates")
    return canonical


def _strict_text(payload: bytes) -> str:
    if not isinstance(payload, bytes):
        raise ChangedPathError("Git path output must be bytes")
    try:
        return payload.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ChangedPathError("Git path output is not valid UTF-8") from exc


def _nul_records(payload: bytes, *, label: str) -> list[str]:
    text = _strict_text(payload)
    if not text:
        return []
    if not text.endswith("\x00"):
        raise ChangedPathError(f"{label} output is not NUL-terminated")
    records = text[:-1].split("\x00")
    if any(not record for record in records):
        raise ChangedPathError(f"{label} output contains an empty record")
    return records


def parse_git_changed_paths(payload: bytes) -> list[str]:
    """Parse ``git diff --name-only -z`` output without Git C-quoting."""
    return canonical_changed_paths(_nul_records(payload, label="Git changed-path"))


def parse_git_numstat(payload: bytes) -> tuple[dict[str, int], list[str]]:
    """Parse ``git diff --numstat -z --no-renames`` output strictly."""
    files = additions = deletions = 0
    paths: list[str] = []
    for record in _nul_records(payload, label="Git numstat"):
        parts = record.split("\t", 2)
        if len(parts) != 3:
            raise ChangedPathError("Git numstat output contains a malformed record")
        added, removed, raw_path = parts
        if added != "-" and not added.isascii():
            raise ChangedPathError("Git numstat additions are malformed")
        if removed != "-" and not removed.isascii():
            raise ChangedPathError("Git numstat deletions are malformed")
        if added != "-" and not added.isdecimal():
            raise ChangedPathError("Git numstat additions are malformed")
        if removed != "-" and not removed.isdecimal():
            raise ChangedPathError("Git numstat deletions are malformed")
        path = canonical_changed_path(raw_path)
        paths.append(path)
        files += 1
        additions += int(added) if added != "-" else 0
        deletions += int(removed) if removed != "-" else 0
    canonical_changed_paths(paths)
    return (
        {"files": files, "additions": additions, "deletions": deletions},
        paths,
    )


def malformed_changed_paths_violation(exc: Exception) -> str:
    """Render an escaped, stable gate violation for a changed-path failure."""
    message = str(exc).encode("unicode_escape").decode("ascii")
    return f"Changed paths are malformed: {message}."


def require_changed_path_list(value: Any) -> list[str]:
    """Validate an untyped editor result at the controller safety boundary."""
    if not isinstance(value, list):
        raise ChangedPathError("expected a list of path strings")
    return canonical_changed_paths(value)
