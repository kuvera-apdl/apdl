"""Tests for the Aider editor's pure helpers + the never-raise contract.

The real agent path (clone → aider → test → push) is integration-untested here;
these cover the deterministic logic and the token-custody env boundary without
invoking ``aider`` or touching the network.
"""

import base64

import pytest

from app.editor.aider_editor import (
    AiderEditor,
    _agent_env,
    _basic_auth_header,
    _build_message,
    _detect_test_cmd,
    _parse_numstat,
)
from app.editor.base import EditRequest


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


def test_detect_test_cmd_python_and_node(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert _detect_test_cmd(tmp_path) == "python -m pytest -q"

    node = tmp_path / "node"
    node.mkdir()
    (node / "package.json").write_text("{}")
    assert _detect_test_cmd(node) == "npm test --silent"


def test_detect_test_cmd_none_when_unknown(tmp_path):
    assert _detect_test_cmd(tmp_path) is None


def test_build_message_appends_constraints():
    msg = _build_message("  Do the thing.  ", ["keep tests green", "no new deps"])
    assert msg == "Do the thing.\n\nConstraints:\n- keep tests green\n- no new deps"


def test_build_message_without_constraints():
    assert _build_message("Just this.", []) == "Just this."


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
