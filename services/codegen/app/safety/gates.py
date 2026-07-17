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
from typing import Any, Sequence

from app.safety.policy import (
    DEFAULT_PROTECTED_PATTERNS,
    EffectiveCodegenSafetyPolicy,
    VerifiedProtectedPathExemption,
)
from app.safety.paths import (
    ChangedPathError,
    canonical_changed_paths,
    malformed_changed_paths_violation,
    require_changed_path_list,
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
            findings.append(
                f"Possible secret matching /{pattern.pattern}/ in the diff."
            )
    return findings


def protected_path_violations(
    paths: list[str],
    protected: tuple[str, ...] = DEFAULT_PROTECTED_PATTERNS,
    verified_exemptions: Sequence[VerifiedProtectedPathExemption] = (),
) -> list[str]:
    """Return a message for each changed path that matches a protected pattern."""
    if any(
        not isinstance(exemption, VerifiedProtectedPathExemption)
        for exemption in verified_exemptions
    ):
        raise TypeError(
            "protected-path exemptions must be VerifiedProtectedPathExemption values"
        )
    paths = canonical_changed_paths(paths)
    exempt_paths = {exemption.path for exemption in verified_exemptions}
    out: list[str] = []
    for path in paths:
        if path in exempt_paths:
            continue
        for pattern in protected:
            if fnmatch(path, pattern) or fnmatch(path, f"*/{pattern}"):
                out.append(
                    f"Change touches protected path {path!r} (matches {pattern!r})."
                )
                break
    return out


def diff_too_large(
    diff_stat: dict[str, Any], *, max_files: int = 50, max_lines: int = 2000
) -> str | None:
    """Return a violation for malformed or over-limit diff statistics."""
    if not isinstance(diff_stat, dict):
        return "Diff statistics are malformed: expected an object."
    required = ("files", "additions", "deletions")
    missing = [name for name in required if name not in diff_stat]
    if missing:
        return "Diff statistics are malformed: missing " + ", ".join(missing) + "."
    for name in required:
        value = diff_stat[name]
        if type(value) is not int or value < 0:
            return (
                f"Diff statistics are malformed: {name} must be a non-negative integer."
            )

    files = diff_stat["files"]
    lines = diff_stat["additions"] + diff_stat["deletions"]
    if files > max_files:
        return f"Diff touches {files} files, exceeding the {max_files}-file limit."
    if lines > max_lines:
        return f"Diff changes {lines} lines, exceeding the {max_lines}-line limit."
    return None


def evaluate_pre_push(
    *,
    diff_stat: dict[str, Any],
    changed_paths: list[str],
    policy: EffectiveCodegenSafetyPolicy,
    diff_text: str = "",
    verified_exemptions: Sequence[VerifiedProtectedPathExemption] = (),
) -> GateResult:
    """Run every gate using only the trusted, resolved effective policy."""
    if not isinstance(policy, EffectiveCodegenSafetyPolicy):
        raise TypeError("policy must be an EffectiveCodegenSafetyPolicy")
    if any(
        not isinstance(exemption, VerifiedProtectedPathExemption)
        for exemption in verified_exemptions
    ):
        raise TypeError(
            "protected-path exemptions must be VerifiedProtectedPathExemption values"
        )
    violations: list[str] = []
    active_exemptions = verified_exemptions
    if verified_exemptions and not policy.runtime_workflow_generation_enabled:
        violations.append(
            "Protected-path exemption rejected: runtime workflow generation "
            "is not enabled by the effective policy."
        )
        active_exemptions = ()

    size = diff_too_large(
        diff_stat,
        max_files=policy.max_files,
        max_lines=policy.max_lines,
    )
    if size:
        violations.append(size)

    try:
        canonical_paths = require_changed_path_list(changed_paths)
    except ChangedPathError as exc:
        violations.append(malformed_changed_paths_violation(exc))
    else:
        violations.extend(
            protected_path_violations(
                canonical_paths,
                policy.protected_paths,
                active_exemptions,
            )
        )

    # Secret scanning is never tenant-configurable and always runs when the diff
    # text has the required type (including for an empty string).
    if isinstance(diff_text, str):
        violations.extend(scan_secrets(diff_text))
    else:
        violations.append("Diff text is malformed: expected a string.")

    return GateResult(passed=not violations, violations=violations)
