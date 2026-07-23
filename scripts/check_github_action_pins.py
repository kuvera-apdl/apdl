#!/usr/bin/env python3
"""Reject mutable third-party references in GitHub workflow/action YAML."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path


USES_KEY = re.compile(r"^\s*(?:-\s*)?uses\s*:")
USES_LINE = re.compile(
    r"""^\s*(?:-\s*)?uses\s*:\s*"""
    r"""(?:"(?P<double>[^"]+)"|'(?P<single>[^']+)'|(?P<bare>[^#\s]+))"""
    r"""\s*(?P<comment>#.*)?$"""
)
FULL_COMMIT_ACTION = re.compile(
    r"^[^/@\s]+/[^/@\s]+(?:/[^@\s]+)*@[0-9a-f]{40}$"
)
IMMUTABLE_DOCKER_ACTION = re.compile(
    r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$"
)
VERSION_COMMENT = re.compile(
    r"^#\s*(?:v[0-9]+(?:\.[0-9]+){0,2}|release/v[0-9]+)\s*$"
)


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    message: str


def _action_yaml_files(repository_root: Path) -> tuple[Path, ...]:
    files: set[Path] = set()
    for directory in (
        repository_root / ".github" / "workflows",
        repository_root / ".github" / "actions",
    ):
        if not directory.is_dir():
            continue
        for suffix in ("*.yml", "*.yaml"):
            files.update(path for path in directory.rglob(suffix) if path.is_file())
    return tuple(sorted(files))


def find_violations(repository_root: Path) -> tuple[Violation, ...]:
    """Return every non-local action reference that is not immutable."""
    root = repository_root.resolve()
    violations: list[Violation] = []
    for path in _action_yaml_files(root):
        relative_path = path.relative_to(root)
        for line_number, raw_line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not USES_KEY.match(raw_line):
                continue
            match = USES_LINE.match(raw_line)
            if match is None:
                violations.append(
                    Violation(
                        relative_path,
                        line_number,
                        "uses must be one static YAML scalar",
                    )
                )
                continue

            reference = (
                match.group("double")
                or match.group("single")
                or match.group("bare")
            )
            comment = match.group("comment")
            if reference.startswith("./"):
                continue
            if IMMUTABLE_DOCKER_ACTION.fullmatch(reference):
                continue
            if not FULL_COMMIT_ACTION.fullmatch(reference):
                violations.append(
                    Violation(
                        relative_path,
                        line_number,
                        "remote action must use a full lowercase 40-character commit SHA",
                    )
                )
                continue
            if comment is None or VERSION_COMMENT.fullmatch(comment) is None:
                violations.append(
                    Violation(
                        relative_path,
                        line_number,
                        "SHA-pinned action must retain one '# vN[.N[.N]]' version comment",
                    )
                )
    return tuple(violations)


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(
            "usage: check_github_action_pins.py [repository-root]",
            file=sys.stderr,
        )
        return 2
    repository_root = (
        Path(argv[1]) if len(argv) == 2 else Path(__file__).resolve().parents[1]
    )
    violations = find_violations(repository_root)
    for violation in violations:
        print(
            f"{violation.path}:{violation.line}: {violation.message}",
            file=sys.stderr,
        )
    if violations:
        print(
            f"GitHub Action pin policy failed with {len(violations)} violation(s)",
            file=sys.stderr,
        )
        return 1
    print("GitHub Action pin policy passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
