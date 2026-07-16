"""Regression tests for exact, NUL-delimited Git changed-path handling."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from app.safety.gates import evaluate_pre_push, protected_path_violations
from app.safety.paths import (
    ChangedPathError,
    parse_git_changed_paths,
    parse_git_numstat,
)
from app.safety.policy import EffectiveCodegenSafetyPolicy


def _policy() -> EffectiveCodegenSafetyPolicy:
    return EffectiveCodegenSafetyPolicy()


def test_changed_paths_preserve_unicode_newline_and_tab_names_exactly():
    expected = [
        ".github/workflows/café.yml",
        ".github/workflows/line\nbreak.yml",
        ".github/workflows/tab\tbreak.yml",
    ]
    payload = b"\x00".join(path.encode("utf-8") for path in expected) + b"\x00"

    parsed = parse_git_changed_paths(payload)

    assert parsed == expected
    violations = protected_path_violations(parsed)
    assert len(violations) == 3
    assert "\\n" in violations[1]
    assert "\\t" in violations[2]


@pytest.mark.parametrize(
    "payload",
    [
        b".github/workflows/ci.yml",
        b".github/workflows/\xff.yml\x00",
        b".github/workflows/ci.yml\x00\x00",
        b"./.github/workflows/ci.yml\x00",
        b"../.github/workflows/ci.yml\x00",
    ],
)
def test_changed_path_parser_fails_closed_for_ambiguous_bytes(payload):
    with pytest.raises(ChangedPathError):
        parse_git_changed_paths(payload)


def test_numstat_preserves_exact_paths_and_counts_binary_files():
    expected = [
        ".github/workflows/café.yml",
        ".github/workflows/line\nbreak.yml",
        ".github/workflows/tab\tbreak.yml",
    ]
    payload = (
        f"4\t1\t{expected[0]}\x002\t0\t{expected[1]}\x00-\t-\t{expected[2]}\x00"
    ).encode()

    stat, parsed = parse_git_numstat(payload)

    assert stat == {"files": 3, "additions": 6, "deletions": 1}
    assert parsed == expected


def test_pre_push_gate_rejects_noncanonical_changed_paths():
    result = evaluate_pre_push(
        diff_stat={"files": 1, "additions": 1, "deletions": 0},
        changed_paths=["./src/app.py"],
        policy=_policy(),
    )

    assert result.passed is False
    assert result.violations == [
        "Changed paths are malformed: changed path must use canonical "
        "repository-relative form: './src/app.py'."
    ]


def test_pre_push_gate_rejects_non_utf8_surrogate_paths():
    result = evaluate_pre_push(
        diff_stat={"files": 1, "additions": 1, "deletions": 0},
        changed_paths=["src/\udcff.py"],
        policy=_policy(),
    )

    assert result.passed is False
    assert "valid UTF-8 text" in result.violations[0]


def test_real_git_nul_output_cannot_quote_protected_unicode_or_control_names(
    tmp_path,
):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is unavailable")
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run([git, "init", "--quiet"], cwd=repository, check=True)
    expected = {
        ".github/workflows/café.yml",
        ".github/workflows/line\nbreak.yml",
        ".github/workflows/tab\tbreak.yml",
    }
    for relative in expected:
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("name: protected\n", encoding="utf-8")
    subprocess.run([git, "add", "--all"], cwd=repository, check=True)

    completed = subprocess.run(
        [git, "diff", "--cached", "--name-only", "-z", "--no-renames"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    parsed = parse_git_changed_paths(completed.stdout)

    assert set(parsed) == expected
    assert len(protected_path_violations(parsed)) == len(expected)
