"""Tests for the pre-edit brief compilation (spec → repo-grounded work order)."""

import json

import pytest

from app.editor.brief import _MIN_BRIEF_CHARS, build_repo_digest, compile_brief
from app.inspection.repository import InspectionPathError

VALID_BRIEF = (
    "## Goal\nDeliver the thing.\n\n"
    "## Scope decisions\n- out of scope: Slack alerting — repo has no Slack wiring\n\n"
    "## Implementation plan\n- edit app/page.tsx\n\n"
    "## Acceptance criteria\n1. The route renders."
) + "x" * _MIN_BRIEF_CHARS


def _make_complete(reply):
    calls = []

    async def complete(system: str, user: str):
        calls.append((system, user))
        return reply

    return complete, calls


def test_digest_lists_files_and_excludes_noise_dirs(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.tsx").write_text("x")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")

    digest = build_repo_digest(tmp_path)

    assert "app/page.tsx" in digest
    assert "node_modules" not in digest
    assert ".git" not in digest


def test_digest_includes_scripts_dependencies_and_readme(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"build": "next build"},
                "dependencies": {"next": "^15"},
                "devDependencies": {"typescript": "^5"},
            }
        )
    )
    (tmp_path / "README.md").write_text("# Demo app\nA fake fintech site.")

    digest = build_repo_digest(tmp_path)

    assert "npm run build" in digest
    assert '"name": "next"' in digest
    assert '"name": "typescript"' in digest
    assert "A fake fintech site." in digest


def test_digest_marks_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr("app.profiling.profiler._MAX_PATHS", 2)
    for name in ("a.ts", "b.ts", "c.ts"):
        (tmp_path / name).write_text("x")

    digest = build_repo_digest(tmp_path)

    assert "truncated" in digest
    assert "c.ts" not in digest


def test_digest_rejects_proc_like_readme_symlink(tmp_path):
    outside = tmp_path.parent / "proc-like-secret"
    outside.write_text(
        "OPENAI_API_KEY=provider-secret-that-must-not-enter-the-digest\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").symlink_to(outside)

    with pytest.raises(
        InspectionPathError, match="repository contains a symbolic link"
    ):
        build_repo_digest(tmp_path)


@pytest.mark.asyncio
async def test_compile_brief_returns_brief_and_feeds_spec_and_digest():
    complete, calls = _make_complete(VALID_BRIEF)

    brief = await compile_brief(
        title="Bot filter",
        spec="Build a bot filter.",
        repo_digest="### Files\napp/page.tsx",
        verification_context="gated on `npm run build`",
        complete=complete,
    )

    assert brief == VALID_BRIEF
    _system, user = calls[0]
    assert "Build a bot filter." in user
    assert "app/page.tsx" in user
    assert "npm run build" in user


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        None,  # model unavailable / call failed
        "## Goal\ntoo short",  # degenerate: under the minimum size
        "no goal section " * 50,  # long but not a brief
    ],
)
async def test_compile_brief_falls_back_on_unusable_output(reply):
    complete, _calls = _make_complete(reply)

    brief = await compile_brief(
        title="t",
        spec="s",
        repo_digest="d",
        verification_context="v",
        complete=complete,
    )

    assert brief is None
