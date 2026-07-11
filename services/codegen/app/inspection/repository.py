"""Bounded and secret-aware read/search primitives for repository evidence."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.inspection.models import EvidenceKind, EvidenceRef, InspectionSnapshot

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

_SECRET_CONTENT_PATTERNS = (
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(rb"\bsk-[A-Za-z0-9]{20,}\b"),
)


class InspectionPathError(ValueError):
    """Raised when a requested path is outside the safe inspection boundary."""


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


def _normalize_relative(path: str) -> str:
    value = path.replace("\\", "/").removeprefix("./")
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or ".." in pure.parts:
        raise InspectionPathError("inspection paths must stay inside the repository")
    return pure.as_posix()


def _secret_shaped_path(path: str) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatch(name, pattern) for pattern in _SECRET_FILE_PATTERNS)


def _binary_shaped_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in _BINARY_SUFFIXES


def _secret_content(data: bytes) -> bool:
    return any(pattern.search(data) for pattern in _SECRET_CONTENT_PATTERNS)


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
    ) -> None:
        root = root.resolve()
        if not root.is_dir():
            raise ValueError(f"repository root is not a directory: {root}")
        if min(max_files, max_file_bytes, max_total_bytes, max_search_results) <= 0:
            raise ValueError("inspection budgets must be positive")
        self.root = root
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_search_results = max_search_results

    def _candidate_paths(self) -> tuple[list[str], bool]:
        paths: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in _EXCLUDED_DIRS
                and not (Path(dirpath) / name).is_symlink()
            )
            for name in sorted(filenames):
                candidate = Path(dirpath) / name
                if candidate.is_symlink():
                    continue
                rel = candidate.relative_to(self.root).as_posix()
                paths.append(rel)
                if len(paths) > self.max_files:
                    return paths[: self.max_files], True
        return paths, False

    def _resolve(self, path: str) -> tuple[str, Path]:
        rel = _normalize_relative(path)
        candidate = self.root / rel
        if candidate.is_symlink():
            raise InspectionPathError(f"symbolic links are not inspectable: {rel}")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise InspectionPathError(
                f"inspection path leaves the repository: {rel}"
            ) from exc
        if not resolved.is_file():
            raise InspectionPathError(f"inspection path is not a file: {rel}")
        if _secret_shaped_path(rel) or _binary_shaped_path(rel):
            raise InspectionPathError(f"inspection path is excluded: {rel}")
        return rel, resolved

    def _inspect_path(self, path: str) -> InspectedText:
        rel, resolved = self._resolve(path)
        with resolved.open("rb") as handle:
            data = handle.read(self.max_file_bytes + 1)
        truncated = len(data) > self.max_file_bytes
        data = data[: self.max_file_bytes]
        if b"\x00" in data:
            raise InspectionPathError(f"binary content is excluded: {rel}")
        if _secret_content(data):
            raise InspectionPathError(f"secret-shaped content is excluded: {rel}")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InspectionPathError(f"non-UTF-8 content is excluded: {rel}") from exc
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
            if total >= self.max_total_bytes:
                truncated = True
                break
            try:
                inspected = self._inspect_path(path)
            except InspectionPathError:
                skipped.append(path)
                continue
            if total + inspected.byte_count > self.max_total_bytes:
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
