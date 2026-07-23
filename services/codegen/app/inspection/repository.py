"""Bounded and secret-aware read/search primitives for repository evidence."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

from app.inspection.models import EvidenceKind, EvidenceRef, InspectionSnapshot
from app.safety.secrets import contains_secret

_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "vendor",
        "dist",
        "build",
        ".next",
        "target",
        "coverage",
        "__pycache__",
    }
)
_VCS_METADATA_DIRS = frozenset({".git", ".hg", ".svn"})
_EXCLUDED_FILES = frozenset({".git"})

_BINARY_SUFFIXES = frozenset(
    {
        ".7z",
        ".a",
        ".avi",
        ".bin",
        ".bmp",
        ".class",
        ".db",
        ".dll",
        ".dylib",
        ".eot",
        ".exe",
        ".gif",
        ".gz",
        ".ico",
        ".jar",
        ".jpeg",
        ".jpg",
        ".lockb",
        ".mov",
        ".mp3",
        ".mp4",
        ".o",
        ".otf",
        ".pdf",
        ".png",
        ".pyc",
        ".so",
        ".sqlite",
        ".tar",
        ".tiff",
        ".ttf",
        ".wav",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
        ".zip",
    }
)

_SECRET_FILE_PATTERNS = (
    ".env",
    ".env.*",
    ".npmrc",
    ".pypirc",
    "*.pem",
    "*.key",
    "*.keystore",
    "*.jks",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "credentials",
    "credentials.*",
    "secrets.json",
    "secrets.yml",
    "secrets.yaml",
)

class InspectionPathError(ValueError):
    """Raised when a requested path is outside the safe inspection boundary."""


class _InspectionContentExcluded(InspectionPathError):
    """An in-repository file whose contents are deliberately not inspectable."""

    def __init__(
        self,
        message: str,
        *,
        byte_count: int = 0,
        truncated: bool = False,
    ) -> None:
        super().__init__(message)
        self.byte_count = byte_count
        self.truncated = truncated


@dataclass(frozen=True)
class InspectedText:
    path: str
    text: str
    content_sha256: str
    byte_count: int
    truncated: bool


@dataclass(frozen=True)
class TextCollection:
    files: dict[str, InspectedText]
    skipped_paths: tuple[str, ...]
    bytes_inspected: int
    truncated: bool


@dataclass(frozen=True)
class RepositoryInventory:
    paths: tuple[str, ...]
    truncated: bool


class RepositoryTextView:
    """One path-inventory snapshot with lazy, no-follow text reads."""

    def __init__(
        self,
        inspector: RepositoryInspector,
        inventory: RepositoryInventory,
    ) -> None:
        self._inspector = inspector
        self.paths = inventory.paths
        self.truncated = inventory.truncated
        self._path_set = frozenset(inventory.paths)
        self._cache: dict[str, InspectedText | None] = {}
        self._bytes_inspected = 0

    @property
    def root(self) -> Path:
        return self._inspector.root

    def contains(self, path: str) -> bool:
        """Return whether the inventory contains this exact regular file."""
        return _normalize_relative(path) in self._path_set

    def inspect(self, path: str) -> InspectedText | None:
        """Return safe text, or ``None`` for deliberately excluded content."""
        rel = _normalize_relative(path)
        if rel not in self._path_set:
            return None
        if rel not in self._cache:
            remaining = self._inspector.max_total_bytes - self._bytes_inspected
            if remaining <= 0:
                raise InspectionPathError(
                    "repository text inspection exceeds the aggregate byte budget"
                )
            try:
                inspected = self._inspector._inspect_path(
                    rel,
                    max_bytes=remaining,
                )
            except _InspectionContentExcluded as exc:
                self._bytes_inspected += exc.byte_count
                self._cache[rel] = None
            else:
                self._bytes_inspected += inspected.byte_count
                if remaining < self._inspector.max_file_bytes and inspected.truncated:
                    raise InspectionPathError(
                        "repository text inspection exceeds the aggregate byte budget"
                    )
                self._cache[rel] = inspected
        return self._cache[rel]

    def text(self, path: str) -> str | None:
        inspected = self.inspect(path)
        return inspected.text if inspected is not None else None


def _normalize_relative(path: str) -> str:
    if "\x00" in path:
        raise InspectionPathError("inspection paths cannot contain NUL bytes")
    value = path.replace("\\", "/").removeprefix("./")
    pure = PurePosixPath(value)
    if not value or not pure.parts or pure.is_absolute() or ".." in pure.parts:
        raise InspectionPathError("inspection paths must stay inside the repository")
    return pure.as_posix()


def _secret_shaped_path(path: str) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatch(name, pattern) for pattern in _SECRET_FILE_PATTERNS)


def _binary_shaped_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in _BINARY_SUFFIXES


def _secret_content(data: bytes) -> bool:
    return contains_secret(data)


def _evidence_id(
    *,
    kind: EvidenceKind,
    path: str,
    content_sha256: str,
    start_line: int | None,
    end_line: int | None,
    source_path: str | None,
    source_line: int | None,
    target_path: str | None,
    symbol: str | None,
) -> str:
    payload = json.dumps(
        {
            "content_sha256": content_sha256,
            "end_line": end_line,
            "kind": kind.value,
            "path": path,
            "source_line": source_line,
            "source_path": source_path,
            "start_line": start_line,
            "symbol": symbol,
            "target_path": target_path,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "ev_" + hashlib.sha256(payload).hexdigest()[:24]


def evidence_ref(
    inspected: InspectedText,
    *,
    kind: EvidenceKind,
    start_line: int | None = None,
    end_line: int | None = None,
    source_path: str | None = None,
    source_line: int | None = None,
    target_path: str | None = None,
    symbol: str | None = None,
    excerpt: str | None = None,
) -> EvidenceRef:
    """Build a stable evidence reference from inspected text."""
    return EvidenceRef(
        evidence_id=_evidence_id(
            kind=kind,
            path=inspected.path,
            content_sha256=inspected.content_sha256,
            start_line=start_line,
            end_line=end_line,
            source_path=source_path,
            source_line=source_line,
            target_path=target_path,
            symbol=symbol,
        ),
        kind=kind,
        path=inspected.path,
        content_sha256=inspected.content_sha256,
        start_line=start_line,
        end_line=end_line,
        source_path=source_path,
        source_line=source_line,
        target_path=target_path,
        symbol=symbol,
        excerpt=excerpt,
        truncated=inspected.truncated,
    )


class RepositoryInspector:
    """Read-only repository view with explicit file, byte, and result budgets."""

    def __init__(
        self,
        root: Path,
        *,
        max_files: int = 5000,
        max_file_bytes: int = 128_000,
        max_total_bytes: int = 4_000_000,
        max_search_results: int = 100,
        max_inventory_entries: int = 20_000,
    ) -> None:
        root = Path(os.path.abspath(os.fspath(root)))
        try:
            root_stat = os.lstat(root)
        except OSError as exc:
            raise ValueError(f"repository root is not a directory: {root}") from exc
        if stat.S_ISLNK(root_stat.st_mode):
            raise ValueError(f"repository root cannot be a symbolic link: {root}")
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError(f"repository root is not a directory: {root}")
        if (
            min(
                max_files,
                max_file_bytes,
                max_total_bytes,
                max_search_results,
                max_inventory_entries,
            )
            <= 0
        ):
            raise ValueError("inspection budgets must be positive")
        self.root = root
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_search_results = max_search_results
        self.max_inventory_entries = max_inventory_entries

    @staticmethod
    def _directory_flags() -> int:
        required = ("O_DIRECTORY", "O_NOFOLLOW")
        if any(not hasattr(os, name) for name in required):
            raise RuntimeError(
                "safe repository inspection requires O_DIRECTORY and O_NOFOLLOW"
            )
        return (
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        )

    @staticmethod
    def _file_flags() -> int:
        if not hasattr(os, "O_NOFOLLOW"):
            raise RuntimeError("safe repository inspection requires O_NOFOLLOW")
        return (
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )

    @contextmanager
    def _open_root(self) -> Iterator[int]:
        try:
            descriptor = os.open(self.root, self._directory_flags())
        except OSError as exc:
            raise InspectionPathError(
                "repository root changed or is no longer safely inspectable"
            ) from exc
        try:
            opened_stat = os.fstat(descriptor)
            if not stat.S_ISDIR(opened_stat.st_mode):
                raise InspectionPathError("repository root is no longer a directory")
            if (opened_stat.st_dev, opened_stat.st_ino) != self._root_identity:
                raise InspectionPathError("repository root changed during inspection")
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _open_file(self, path: str) -> Iterator[int]:
        """Open one repository file without following any path component."""
        rel = _normalize_relative(path)
        descriptors: list[int] = []
        try:
            with self._open_root() as root_descriptor:
                parent_descriptor = root_descriptor
                for component in PurePosixPath(rel).parts[:-1]:
                    try:
                        descriptor = os.open(
                            component,
                            self._directory_flags(),
                            dir_fd=parent_descriptor,
                        )
                    except OSError as exc:
                        raise InspectionPathError(
                            f"inspection path has an unsafe directory component: {rel}"
                        ) from exc
                    descriptors.append(descriptor)
                    parent_descriptor = descriptor

                try:
                    file_descriptor = os.open(
                        PurePosixPath(rel).name,
                        self._file_flags(),
                        dir_fd=parent_descriptor,
                    )
                except OSError as exc:
                    raise InspectionPathError(
                        f"inspection path is not a safe regular file: {rel}"
                    ) from exc
                descriptors.append(file_descriptor)
                if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                    raise InspectionPathError(
                        f"inspection path is not a regular file: {rel}"
                    )
                yield file_descriptor
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _candidate_paths(self) -> tuple[list[str], bool]:
        paths: list[str] = []
        scanned_entries = 0
        truncated = False

        def walk(
            directory_descriptor: int,
            prefix: PurePosixPath,
        ) -> bool:
            nonlocal scanned_entries, truncated
            try:
                with os.scandir(directory_descriptor) as directory_entries:
                    entries = sorted(directory_entries, key=lambda item: item.name)
            except OSError as exc:
                display = prefix.as_posix() if prefix.parts else "."
                raise InspectionPathError(
                    f"repository directory is not safely inspectable: {display}"
                ) from exc

            for entry in entries:
                scanned_entries += 1
                if scanned_entries > self.max_inventory_entries:
                    truncated = True
                    return True
                if "\\" in entry.name:
                    raise InspectionPathError(
                        "repository entry names cannot contain backslashes"
                    )
                rel_path = prefix / entry.name
                rel = rel_path.as_posix()
                try:
                    mode = os.stat(
                        entry.name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    ).st_mode
                except OSError as exc:
                    raise InspectionPathError(
                        f"repository entry changed during inspection: {rel}"
                    ) from exc
                if stat.S_ISLNK(mode):
                    raise InspectionPathError(
                        f"repository contains a symbolic link: {rel}"
                    )
                if stat.S_ISDIR(mode):
                    if entry.name in _VCS_METADATA_DIRS:
                        continue
                    if entry.name in _EXCLUDED_DIRS:
                        continue
                    try:
                        child_descriptor = os.open(
                            entry.name,
                            self._directory_flags(),
                            dir_fd=directory_descriptor,
                        )
                    except OSError as exc:
                        raise InspectionPathError(
                            f"repository directory changed during inspection: {rel}"
                        ) from exc
                    try:
                        if walk(child_descriptor, rel_path):
                            return True
                    finally:
                        os.close(child_descriptor)
                    continue
                if not stat.S_ISREG(mode):
                    raise InspectionPathError(
                        f"repository contains a non-regular entry: {rel}"
                    )
                if entry.name in _EXCLUDED_FILES:
                    continue
                if len(paths) >= self.max_files:
                    truncated = True
                    return True
                paths.append(rel)
            return False

        with self._open_root() as root_descriptor:
            walk(root_descriptor, PurePosixPath())
        paths.sort()
        return paths, truncated

    def inventory(self) -> RepositoryInventory:
        """Return a bounded path inventory, rejecting every visible symlink."""
        paths, truncated = self._candidate_paths()
        return RepositoryInventory(paths=tuple(paths), truncated=truncated)

    def text_view(self) -> RepositoryTextView:
        """Return one inventory-bound, lazily inspected repository text view."""
        return RepositoryTextView(self, self.inventory())

    def inspect_text(self, path: str) -> InspectedText:
        """Read one complete bounded UTF-8 file through the no-follow boundary."""
        return self._inspect_path(path)

    def _inspect_path(
        self,
        path: str,
        *,
        max_bytes: int | None = None,
    ) -> InspectedText:
        rel = _normalize_relative(path)
        if _secret_shaped_path(rel) or _binary_shaped_path(rel):
            raise _InspectionContentExcluded(f"inspection path is excluded: {rel}")
        limit = self.max_file_bytes
        if max_bytes is not None:
            if max_bytes <= 0:
                raise InspectionPathError(
                    "repository text inspection exceeds the aggregate byte budget"
                )
            limit = min(limit, max_bytes)
        with self._open_file(rel) as descriptor:
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
        truncated = len(data) > limit
        data = data[:limit]
        if b"\x00" in data:
            raise _InspectionContentExcluded(
                f"binary content is excluded: {rel}",
                byte_count=len(data),
                truncated=truncated,
            )
        if _secret_content(data):
            raise _InspectionContentExcluded(
                f"secret-shaped content is excluded: {rel}",
                byte_count=len(data),
                truncated=truncated,
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _InspectionContentExcluded(
                f"non-UTF-8 content is excluded: {rel}",
                byte_count=len(data),
                truncated=truncated,
            ) from exc
        return InspectedText(
            path=rel,
            text=text,
            content_sha256=hashlib.sha256(data).hexdigest(),
            byte_count=len(data),
            truncated=truncated,
        )

    def collect_texts(self) -> TextCollection:
        """Collect safe text files once, bounded by the repository byte budget."""
        candidates, truncated = self._candidate_paths()
        files: dict[str, InspectedText] = {}
        skipped: list[str] = []
        total = 0
        for path in candidates:
            remaining = self.max_total_bytes - total
            if remaining <= 0:
                truncated = True
                break
            try:
                inspected = self._inspect_path(path, max_bytes=remaining)
            except _InspectionContentExcluded as exc:
                skipped.append(path)
                total += exc.byte_count
                if remaining < self.max_file_bytes and exc.truncated:
                    truncated = True
                    break
                continue
            if remaining < self.max_file_bytes and inspected.truncated:
                truncated = True
                break
            files[path] = inspected
            total += inspected.byte_count
        return TextCollection(
            files=dict(sorted(files.items())),
            skipped_paths=tuple(sorted(set(skipped))),
            bytes_inspected=total,
            truncated=truncated,
        )

    def read(
        self,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> EvidenceRef:
        """Read a focused line range and return content-addressed evidence."""
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise ValueError("invalid focused read line range")
        inspected = self._inspect_path(path)
        lines = inspected.text.splitlines()
        if end_line is None:
            end_line = max(start_line, min(len(lines), start_line + 199))
        else:
            end_line = max(start_line, min(end_line, max(len(lines), start_line)))
        excerpt = "\n".join(lines[start_line - 1 : end_line])[:4000]
        return evidence_ref(
            inspected,
            kind=EvidenceKind.file,
            start_line=start_line,
            end_line=end_line,
            excerpt=excerpt,
        )

    def search(
        self,
        query: str,
        *,
        symbol: bool = False,
        path_globs: tuple[str, ...] = (),
        max_results: int | None = None,
        case_sensitive: bool = True,
    ) -> list[EvidenceRef]:
        """Perform a bounded literal or identifier search over safe text files.

        Arbitrary regular expressions are intentionally not accepted: literal
        and identifier searches cannot trigger catastrophic regex backtracking.
        """
        if not query or len(query) > 200 or "\n" in query:
            raise ValueError("search query must be 1-200 characters on one line")
        requested_limit = (
            self.max_search_results if max_results is None else max_results
        )
        limit = min(requested_limit, self.max_search_results)
        if limit <= 0:
            raise ValueError("max_results must be positive")
        collection = self.collect_texts()
        flags = 0 if case_sensitive else re.IGNORECASE
        expression = re.compile(
            rf"\b{re.escape(query)}\b" if symbol else re.escape(query), flags
        )
        results: list[EvidenceRef] = []
        for path, inspected in collection.files.items():
            if path_globs and not any(
                fnmatch.fnmatch(path, glob) for glob in path_globs
            ):
                continue
            for line_number, line in enumerate(inspected.text.splitlines(), start=1):
                if not expression.search(line):
                    continue
                results.append(
                    evidence_ref(
                        inspected,
                        kind=EvidenceKind.symbol if symbol else EvidenceKind.search,
                        start_line=line_number,
                        end_line=line_number,
                        symbol=query if symbol else None,
                        excerpt=line[:1000],
                    )
                )
                if len(results) >= limit:
                    return results
        return results

    def snapshot(self) -> InspectionSnapshot:
        """Return a stable inventory without exposing repository contents."""
        collection = self.collect_texts()
        evidence = [
            evidence_ref(inspected, kind=EvidenceKind.file)
            for inspected in collection.files.values()
        ]
        return InspectionSnapshot(
            evidence=evidence,
            skipped_paths=list(collection.skipped_paths),
            bytes_inspected=collection.bytes_inspected,
            truncated=collection.truncated,
        )
