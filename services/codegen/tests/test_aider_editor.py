"""Tests for the Aider editor's pure helpers + the never-raise contract.

The real agent path (clone → aider → test → push) is integration-untested here;
these cover the deterministic logic and the token-custody env boundary without
invoking ``aider`` or touching the network.
"""

import base64
from pathlib import Path

import pytest

from app.editor.aider_editor import (
    AiderEditor,
    _agent_env,
    _basic_auth_header,
    _build_message,
    _detect_test_cmd,
    _model_settings_yaml,
    _parse_numstat,
)
from app.editor.base import EditRequest
from app.editor.conventions import CONVENTIONS_MD


def test_parse_numstat_sums_files_and_lines():
    out = "10\t2\tapp/a.py\n5\t0\tapp/b.py\n"
    assert _parse_numstat(out) == {"files": 2, "additions": 15, "deletions": 2}


def test_parse_numstat_counts_binary_as_touched_zero_lines():
    out = "-\t-\tassets/logo.png\n3\t1\tREADME.md\n"
    assert _parse_numstat(out) == {"files": 2, "additions": 3, "deletions": 1}


def test_parse_numstat_ignores_malformed_lines():
    assert _parse_numstat("garbage\n\n") == {"files": 0, "additions": 0, "deletions": 0}


def test_detect_test_cmd_prefers_makefile_test_target(tmp_path):
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
    (tmp_path / "package.json").write_text("{}")
    assert _detect_test_cmd(tmp_path) == "make test"


def test_detect_test_cmd_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert _detect_test_cmd(tmp_path) == "python -m pytest -q"


def test_detect_test_cmd_npm_prefers_test_script(tmp_path):
    # A real `test` script → install then test (fresh clone has no node_modules).
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    assert _detect_test_cmd(tmp_path) == (
        "npm install --no-audit --no-fund --silent && npm test --silent"
    )


def test_detect_test_cmd_npm_falls_back_to_build(tmp_path):
    # No `test` script but a `build` (e.g. a Next.js app) → build is the gate.
    (tmp_path / "package.json").write_text('{"scripts": {"build": "next build"}}')
    assert _detect_test_cmd(tmp_path) == (
        "npm install --no-audit --no-fund --silent && npm run build"
    )


def test_detect_test_cmd_npm_skips_without_test_or_build(tmp_path):
    # No usable script → skip the verify step rather than run a doomed `npm test`.
    (tmp_path / "package.json").write_text("{}")
    assert _detect_test_cmd(tmp_path) is None


def test_detect_test_cmd_none_when_unknown(tmp_path):
    assert _detect_test_cmd(tmp_path) is None


def test_build_message_appends_constraints():
    msg = _build_message("  Do the thing.  ", ["keep tests green", "no new deps"])
    assert msg == "Do the thing.\n\nConstraints:\n- keep tests green\n- no new deps"


def test_build_message_without_constraints():
    assert _build_message("Just this.", []) == "Just this."


def test_model_settings_yaml_disables_temperature():
    out = _model_settings_yaml("claude-opus-4-8")
    assert '- name: "claude-opus-4-8"' in out
    assert "use_temperature: false" in out


def test_basic_auth_header_encodes_x_access_token():
    header = _basic_auth_header("ghs_tok")
    assert header.startswith("AUTHORIZATION: basic ")
    encoded = header.split("basic ", 1)[1]
    assert base64.b64decode(encoded).decode() == "x-access-token:ghs_tok"


def test_agent_env_forwards_llm_keys_but_not_github_or_apdl_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xyz")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-xyz")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----")
    monkeypatch.setenv("APDL_INTERNAL_TOKEN", "internal")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://nope")

    env = _agent_env()

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-xyz"
    assert env["OPENAI_API_KEY"] == "sk-openai-xyz"
    assert "GITHUB_APP_PRIVATE_KEY" not in env
    assert "APDL_INTERNAL_TOKEN" not in env
    assert "POSTGRES_URL" not in env


@pytest.mark.asyncio
async def test_implement_never_raises_on_unexpected_fault(monkeypatch, tmp_path):
    """An ordinary fault must come back as success=False, not an exception."""
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))

    async def boom(*_args, **_kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(editor, "_git", boom)  # blow up at the first git call

    result = await editor.implement(
        EditRequest(
            repo="acme/widgets",
            base_branch="main",
            branch="apdl/x",
            token="ghs_tok",
            title="x",
            spec="do a thing",
        )
    )

    assert result.success is False
    assert result.branch == "apdl/x"
    assert "kaboom" in (result.error or "")
    # The throwaway workdir is cleaned up even on the failure path.
    assert not list(tmp_path.iterdir())


async def _capture_aider_argv(editor: AiderEditor, monkeypatch) -> list[str]:
    """Drive implement() far enough to capture the aider invocation's argv."""
    captured: dict = {}

    async def fake_git(*_args, **_kwargs):
        return 0, ""  # clone / checkout / config all succeed

    async def fake_exec(argv, **_kwargs):
        captured["argv"] = argv
        return 1, "stop"  # non-zero bails right after the aider call

    monkeypatch.setattr(editor, "_git", fake_git)
    monkeypatch.setattr(editor, "_exec", fake_exec)

    await editor.implement(
        EditRequest(
            repo="acme/widgets", base_branch="main", branch="apdl/x",
            token="ghs_tok", title="x", spec="do a thing",
        )
    )
    return captured["argv"]


@pytest.mark.asyncio
async def test_aider_argv_caches_prompts_by_default(monkeypatch, tmp_path):
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    argv = await _capture_aider_argv(editor, monkeypatch)
    assert "--cache-prompts" in argv


@pytest.mark.asyncio
async def test_aider_argv_omits_cache_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_CACHE_PROMPTS", "false")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    argv = await _capture_aider_argv(editor, monkeypatch)
    assert "--cache-prompts" not in argv


@pytest.mark.asyncio
async def test_aider_argv_reads_conventions_by_default(monkeypatch, tmp_path):
    # Keep the workdir so the written CONVENTIONS.md survives for assertion
    # (it is otherwise rmtree'd in the run's finally block).
    monkeypatch.setenv("CODEGEN_KEEP_WORKDIR", "true")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    argv = await _capture_aider_argv(editor, monkeypatch)
    # --read points at a CONVENTIONS.md written outside the cloned repo so it
    # never enters the diff, and the file actually carries the house rules.
    assert "--read" in argv
    read_path = Path(argv[argv.index("--read") + 1])
    assert read_path.name == "CONVENTIONS.md"
    assert "repo" not in read_path.parts  # outside repo_dir → not in the diff
    assert read_path.read_text(encoding="utf-8") == CONVENTIONS_MD


@pytest.mark.asyncio
async def test_aider_argv_omits_conventions_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_CONVENTIONS", "false")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    argv = await _capture_aider_argv(editor, monkeypatch)
    assert "--read" not in argv
