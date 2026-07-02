"""Deterministic pre-push safety gates.

These run on the produced diff BEFORE the branch is pushed (inside the editor,
on the full diff text) and again as a backstop before the PR is opened (in the
job runner) — outside the editing agent's control, so a prompt-injected or
careless edit cannot bypass them. They are pure functions over the diff: its
size, the paths it touches, and — when the diff text is available — secret
patterns. Never trust the LLM to self-police; these gates are the backstop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any

#: Paths an autonomous change must never touch (CI config, keys, env files).
DEFAULT_PROTECTED_PATTERNS: tuple[str, ...] = (
    ".github/workflows/*",
    "*.pem",
    "*.key",
    "id_rsa*",
    ".env",
    ".env.*",
)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),  # GitHub tokens
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),  # generic secret keys
)


@dataclass
class GateResult:
    passed: bool
    violations: list[str] = field(default_factory=list)


def scan_secrets(diff_text: str) -> list[str]:
    """Return findings for any secret-shaped string in the diff text."""
    findings: list[str] = []
    for pattern in _SECRET_PATTERNS:
        if pattern.search(diff_text):
            findings.append(f"Possible secret matching /{pattern.pattern}/ in the diff.")
    return findings


def protected_path_violations(
    paths: list[str], protected: tuple[str, ...] = DEFAULT_PROTECTED_PATTERNS
) -> list[str]:
    """Return a message for each changed path that matches a protected pattern."""
    out: list[str] = []
    for path in paths:
        for pattern in protected:
            if fnmatch(path, pattern) or fnmatch(path, f"*/{pattern}"):
                out.append(f"Change touches protected path '{path}' (matches '{pattern}').")
                break
    return out


def diff_too_large(
    diff_stat: dict[str, Any], *, max_files: int = 50, max_lines: int = 2000
) -> str | None:
    """Return a message if the diff exceeds the file/line blast-radius limits."""
    if not isinstance(diff_stat, dict):
        return None
    files = diff_stat.get("files", 0)
    lines = diff_stat.get("additions", 0) + diff_stat.get("deletions", 0)
    if files > max_files:
        return f"Diff touches {files} files, exceeding the {max_files}-file limit."
    if lines > max_lines:
        return f"Diff changes {lines} lines, exceeding the {max_lines}-line limit."
    return None


def evaluate_pre_push(
    *,
    diff_stat: dict[str, Any],
    changed_paths: list[str],
    diff_text: str = "",
    policy: dict[str, Any] | None = None,
) -> GateResult:
    """Run every pre-push gate and aggregate the violations.

    ``policy`` (from the repo connection) may override ``protected_paths``,
    ``max_files``, and ``max_lines``.
    """
    policy = policy or {}
    protected = tuple(policy.get("protected_paths", DEFAULT_PROTECTED_PATTERNS))
    violations: list[str] = []

    size = diff_too_large(
        diff_stat,
        max_files=policy.get("max_files", 50),
        max_lines=policy.get("max_lines", 2000),
    )
    if size:
        violations.append(size)
    violations.extend(protected_path_violations(changed_paths, protected))
    if diff_text:
        violations.extend(scan_secrets(diff_text))

    return GateResult(passed=not violations, violations=violations)
