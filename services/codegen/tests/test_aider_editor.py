"""Tests for the Aider editor's pure helpers + the never-raise contract.

The real agent path (clone → aider → test → push) is integration-untested here;
these cover the deterministic logic and the token-custody env boundary without
invoking ``aider`` or touching the network.
"""

import base64
from pathlib import Path

import pytest

import app.editor.aider_editor as aider_editor
from app.editor.aider_editor import (
    AiderEditor,
    _RepoProbe,
    _agent_env,
    _basic_auth_header,
    _build_message,
    _capability_preamble,
    _detect_test_cmd,
    _model_settings_yaml,
    _npm_verify_cmd,
    _parse_numstat,
    _probe_repo,
    _repo_has_test_runner,
    _tail,
)
from app.editor.base import EditRequest
from app.editor.conventions import CONVENTIONS_MD


def test_tail_returns_full_text_when_under_limit():
    assert _tail("short error", limit=800) == "short error"


def test_tail_snaps_to_line_boundary_and_marks_truncation():
    # A long first line then a clean second line; the tail budget lands inside
    # the first line, so the excerpt must drop that partial line, not begin
    # mid-word, and must announce how much was dropped.
    text = "verification failed: apdl-oss/sdk/dist/apdl.esm.js\n" + "x" * 50 + "\nreal error line"
    out = _tail(text, limit=20)
    assert out.startswith("[…truncated ")
    body = out.split("\n", 1)[1]
    # No partial leading line survived: the body starts at a real line boundary.
    assert "apdl-oss/sdk" not in body
    assert body.endswith("real error line")


def test_tail_strips_before_measuring():
    assert _tail("   hi   ", limit=800) == "hi"


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


def test_npm_verify_chains_build_and_test_with_typecheck(tmp_path):
    # test + build present → install, then the build (its own type-check), then test.
    (tmp_path / "package.json").write_text(
        '{"scripts": {"build": "next build", "test": "vitest run"}}'
    )
    assert _npm_verify_cmd(tmp_path / "package.json") == (
        "npm install --no-audit --no-fund --silent && npm run build && npm test --silent"
    )


def test_npm_verify_adds_tsc_typecheck_when_no_build_script(tmp_path):
    # A TS repo with tests but no build script still gets a type gate — a missing
    # import must not slip past unit tests (the PR #7 failure mode).
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    (tmp_path / "tsconfig.json").write_text("{}")
    assert _npm_verify_cmd(tmp_path / "package.json") == (
        "npm install --no-audit --no-fund --silent "
        "&& npx --no-install tsc --noEmit && npm test --silent"
    )


def test_npm_verify_none_when_nothing_to_check(tmp_path):
    # No build, no tsconfig, no test → nothing meaningful to verify.
    (tmp_path / "package.json").write_text('{"scripts": {"lint": "eslint"}}')
    assert _npm_verify_cmd(tmp_path / "package.json") is None


def test_has_test_runner_via_test_script(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    assert _repo_has_test_runner(tmp_path) is True


def test_has_test_runner_via_dev_dependency(tmp_path):
    # A runner installed but not scripted still counts as "the repo can test".
    (tmp_path / "package.json").write_text('{"devDependencies": {"vitest": "^1.0.0"}}')
    assert _repo_has_test_runner(tmp_path) is True


def test_has_test_runner_false_for_next_app_without_tests(tmp_path):
    # The PR #7 repo shape: build/lint scripts, no runner anywhere.
    (tmp_path / "package.json").write_text(
        '{"scripts": {"build": "next build", "lint": "eslint"}}'
    )
    assert _repo_has_test_runner(tmp_path) is False


def test_capability_preamble_warns_when_no_runner():
    text = _capability_preamble(has_test_runner=False, verify_cmd="npm run build")
    assert "NO test framework" in text
    assert "npm run build" in text
    assert "do NOT import a test library" in text.lower() or "not import a test" in text.lower()


def test_capability_preamble_invites_tests_when_runner_present():
    text = _capability_preamble(has_test_runner=True, verify_cmd="npm test")
    assert "HAS a test framework" in text
    assert "ALREADY depends on" in text


def test_probe_repo_prefers_override_cmd_but_keeps_repo_runner_signal(tmp_path):
    # A Next.js app (no runner) with an operator-supplied gate command.
    (tmp_path / "package.json").write_text('{"scripts": {"build": "next build"}}')
    probe = _probe_repo(tmp_path, override_cmd="make ci")
    assert probe.verify_cmd == "make ci"  # override wins as the gate
    assert probe.has_test_runner is False  # signal still read from the repo
    assert "NO test framework" in probe.preamble


def test_build_message_prepends_preamble():
    msg = _build_message("Do the thing.", ["keep it small"], preamble="## Context\nrepo has no tests")
    assert msg.startswith("## Context\nrepo has no tests")
    assert msg.endswith("Constraints:\n- keep it small")
    assert "Do the thing." in msg


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
    # Also silence the SDK reference so the only --read source is conventions.
    monkeypatch.setenv("CODEGEN_SDK_REFERENCE", "false")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    argv = await _capture_aider_argv(editor, monkeypatch)
    assert "--read" not in argv


def _fake_probe_with_refs(refs):
    """A probe with no verify context but the given SDK references."""
    return _RepoProbe(
        verify_cmd=None, has_test_runner=False, preamble="", sdk_references=refs
    )


@pytest.mark.asyncio
async def test_aider_argv_reads_matching_sdk_reference(monkeypatch, tmp_path):
    # A repo whose manifest calls for the JS SDK gets its reference --read in,
    # written outside the clone (so it never enters the diff) with the real body.
    monkeypatch.setenv("CODEGEN_KEEP_WORKDIR", "true")
    monkeypatch.setattr(
        aider_editor,
        "_probe_repo",
        lambda *_a, **_k: _fake_probe_with_refs((("APDL_SDK_JS.md", "JS REF BODY"),)),
    )
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    argv = await _capture_aider_argv(editor, monkeypatch)

    read_paths = [Path(argv[i + 1]) for i, a in enumerate(argv) if a == "--read"]
    js_ref = next(p for p in read_paths if p.name == "APDL_SDK_JS.md")
    assert "repo" not in js_ref.parts  # outside repo_dir → not in the diff
    assert js_ref.read_text(encoding="utf-8") == "JS REF BODY"


@pytest.mark.asyncio
async def test_aider_argv_omits_sdk_reference_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_SDK_REFERENCE", "false")
    monkeypatch.setattr(
        aider_editor,
        "_probe_repo",
        lambda *_a, **_k: _fake_probe_with_refs((("APDL_SDK_JS.md", "JS REF BODY"),)),
    )
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    argv = await _capture_aider_argv(editor, monkeypatch)

    read_names = [Path(argv[i + 1]).name for i, a in enumerate(argv) if a == "--read"]
    assert "APDL_SDK_JS.md" not in read_names


@pytest.mark.asyncio
async def test_aider_argv_omits_sdk_reference_when_repo_has_none(monkeypatch, tmp_path):
    # No APDL SDK in the repo ⇒ no reference attached (the faked clone is empty).
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    argv = await _capture_aider_argv(editor, monkeypatch)
    read_names = [Path(argv[i + 1]).name for i, a in enumerate(argv) if a == "--read"]
    assert not any(n.startswith("APDL_SDK_") for n in read_names)


@pytest.mark.asyncio
async def test_aider_message_carries_verification_context(monkeypatch, tmp_path):
    # The per-repo testing reality must reach the agent's message. The clone is
    # faked (empty), so the probe reports no runner and no verify command.
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    argv = await _capture_aider_argv(editor, monkeypatch)
    message = argv[argv.index("--message") + 1]
    assert "Repository verification context" in message
    assert "NO test framework" in message


@pytest.mark.asyncio
async def test_implement_fails_closed_when_unverifiable(monkeypatch, tmp_path):
    """No detectable gate + CODEGEN_REQUIRE_VERIFY on ⇒ no PR, clean failure."""
    monkeypatch.delenv("CODEGEN_REQUIRE_VERIFY", raising=False)  # default: on
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))

    async def fake_git(*_args, **_kwargs):
        return 0, ""  # clone / checkout / config succeed

    async def fake_exec(_argv, **_kwargs):
        return 0, "done"  # aider "succeeds" (empty clone → nothing to verify)

    monkeypatch.setattr(editor, "_git", fake_git)
    monkeypatch.setattr(editor, "_exec", fake_exec)

    result = await editor.implement(
        EditRequest(
            repo="acme/widgets", base_branch="main", branch="apdl/x",
            token="ghs_tok", title="x", spec="do a thing",
        )
    )

    assert result.success is False
    assert "unverified" in (result.error or "")


@pytest.mark.asyncio
async def test_implement_opts_out_of_verify_gate_when_disabled(monkeypatch, tmp_path):
    """CODEGEN_REQUIRE_VERIFY=false ⇒ the unverifiable-repo path no longer blocks.

    It fails later for a different reason (the faked clone has no diff), proving
    the fail-closed guard was bypassed rather than the run stopping at it.
    """
    monkeypatch.setenv("CODEGEN_REQUIRE_VERIFY", "false")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))

    async def fake_git(*_args, **_kwargs):
        return 0, ""

    async def fake_exec(_argv, **_kwargs):
        return 0, "done"

    monkeypatch.setattr(editor, "_git", fake_git)
    monkeypatch.setattr(editor, "_exec", fake_exec)

    result = await editor.implement(
        EditRequest(
            repo="acme/widgets", base_branch="main", branch="apdl/x",
            token="ghs_tok", title="x", spec="do a thing",
        )
    )

    assert result.success is False
    assert "unverified" not in (result.error or "")
    assert "no changes" in (result.error or "")


# --- The edit loop: brief → aider → verify → review, with feedback retries ----


def _request(test_cmd: str | None = "make test") -> EditRequest:
    return EditRequest(
        repo="acme/widgets", base_branch="main", branch="apdl/x",
        token="ghs_tok", title="Bot filter", spec="Build a bot filter.",
        test_cmd=test_cmd,
    )


class _Pipeline:
    """Scripted git/aider/test doubles that drive implement() end-to-end."""

    def __init__(self, editor, monkeypatch, *, test_results=None):
        self.aider_messages: list[str] = []
        self.pushed = False
        self._test_results = list(test_results or [])

        async def fake_git(cwd, args, **_kwargs):
            if args[0] == "diff" and "--name-only" in args:
                return 0, "app/x.ts\n"
            if args[0] == "diff" and "--numstat" in args:
                return 0, "5\t1\tapp/x.ts\n"
            if args[0] == "diff":
                return 0, "diff --git a/app/x.ts b/app/x.ts\n+new"
            if args[0] == "push":
                self.pushed = True
            return 0, ""  # clone / checkout / config

        async def fake_exec(argv, **_kwargs):
            self.aider_messages.append(argv[argv.index("--message") + 1])
            return 0, "aider ok"

        async def fake_run_tests(_repo_dir, _test_cmd):
            return self._test_results.pop(0) if self._test_results else (True, "")

        monkeypatch.setattr(editor, "_git", fake_git)
        monkeypatch.setattr(editor, "_exec", fake_exec)
        monkeypatch.setattr(editor, "_run_tests", fake_run_tests)


def _routing_complete(brief_reply=None, review_replies=None):
    """One completer serving both auxiliary passes, routed by system prompt."""
    replies = list(review_replies or [])

    async def complete(system: str, _user: str):
        if "engineering briefs" in system:
            return brief_reply
        return replies.pop(0) if replies else '{"approved": true}'

    return complete


_BRIEF = (
    "## Goal\nShip the bot filter.\n\n## Scope decisions\n- none\n\n"
    "## Implementation plan\n- edit app/x.ts\n\n## Acceptance criteria\n1. filter works"
) + "." * 200


@pytest.mark.asyncio
async def test_edit_loop_replaces_spec_with_compiled_brief(monkeypatch, tmp_path):
    editor = AiderEditor(
        model="claude-opus-4-8", workdir_base=str(tmp_path),
        complete=_routing_complete(brief_reply=_BRIEF),
    )
    pipeline = _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request())

    assert result.success is True
    assert pipeline.pushed is True
    assert len(pipeline.aider_messages) == 1
    assert "Ship the bot filter." in pipeline.aider_messages[0]
    assert "Build a bot filter." not in pipeline.aider_messages[0]


@pytest.mark.asyncio
async def test_edit_loop_keeps_raw_spec_when_brief_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    editor = AiderEditor(
        model="claude-opus-4-8", workdir_base=str(tmp_path),
        complete=_routing_complete(),
    )
    pipeline = _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request())

    assert result.success is True
    assert "Build a bot filter." in pipeline.aider_messages[0]


@pytest.mark.asyncio
async def test_verify_failure_retries_with_the_failing_output(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    monkeypatch.setenv("CODEGEN_REVIEW", "false")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    pipeline = _Pipeline(
        editor, monkeypatch,
        test_results=[(False, "Module not found: hashBucket"), (True, "")],
    )

    result = await editor.implement(_request())

    assert result.success is True
    assert len(pipeline.aider_messages) == 2
    retry = pipeline.aider_messages[1]
    assert "FAILED the verification command" in retry
    assert "hashBucket" in retry
    assert "do not revert" in retry


@pytest.mark.asyncio
async def test_verify_failure_exhausts_retries_then_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    monkeypatch.setenv("CODEGEN_REVIEW", "false")
    monkeypatch.setenv("CODEGEN_EDIT_RETRIES", "1")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    pipeline = _Pipeline(
        editor, monkeypatch, test_results=[(False, "boom one"), (False, "boom two")]
    )

    result = await editor.implement(_request())

    assert result.success is False
    assert "verification failed" in (result.error or "")
    assert "boom two" in (result.error or "")
    assert len(pipeline.aider_messages) == 2
    assert pipeline.pushed is False


@pytest.mark.asyncio
async def test_review_rejection_retries_with_instructions(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    rejected = (
        '{"approved": false, "problems": ["link targets a route that does not exist"],'
        ' "fix_instructions": "Create the page and wire it in."}'
    )
    editor = AiderEditor(
        model="claude-opus-4-8", workdir_base=str(tmp_path),
        complete=_routing_complete(review_replies=[rejected, '{"approved": true}']),
    )
    pipeline = _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request())

    assert result.success is True
    assert len(pipeline.aider_messages) == 2
    retry = pipeline.aider_messages[1]
    assert "REJECTED" in retry
    assert "Create the page and wire it in." in retry


@pytest.mark.asyncio
async def test_review_rejection_without_retries_fails_the_changeset(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    monkeypatch.setenv("CODEGEN_EDIT_RETRIES", "0")
    rejected = '{"approved": false, "problems": ["a token diff"], "fix_instructions": "x"}'
    editor = AiderEditor(
        model="claude-opus-4-8", workdir_base=str(tmp_path),
        complete=_routing_complete(review_replies=[rejected]),
    )
    pipeline = _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request())

    assert result.success is False
    assert "quality review rejected" in (result.error or "")
    assert "a token diff" in (result.error or "")
    assert pipeline.pushed is False


@pytest.mark.asyncio
async def test_pipeline_runs_without_any_completer(monkeypatch, tmp_path):
    """No LiteLLM / no key ⇒ both auxiliary passes skip and the edit still ships."""
    monkeypatch.setattr(aider_editor, "resolve_completer", lambda: None)
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    pipeline = _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request())

    assert result.success is True
    assert pipeline.pushed is True
    assert "Build a bot filter." in pipeline.aider_messages[0]
