"""Tests for the Aider editor's pure helpers + the never-raise contract.

The real agent path (clone → aider → test → candidate patch) is
integration-untested here; these cover the deterministic logic and the
token-custody env boundary without invoking ``aider`` or touching the network.
"""

import base64
import json
import re
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
    _model_settings_yaml,
    _parse_numstat,
    _probe_repo,
    _tail,
)
from app.editor.base import EditRequest
from app.editor.conventions import CONVENTIONS_MD
from app.profiling.models import (
    CIWorkflow,
    CommandKind,
    Dependency,
    PackageBoundary,
    PackageManager,
    RepoCommand,
    RepoProfile,
    TestFacility as ProfileTestFacility,
)
from app.runtime.models import (
    RUNTIME_ACCEPTANCE_WORKFLOW_PATH,
    RuntimeAcceptancePolicy,
)
from app.safety.policy import (
    PlatformCodegenSafetyPolicy,
    TenantCodegenConnectionPolicy,
    TenantCodegenGatesPolicy,
    resolve_effective_policy,
)
from app.verification import CoverageDisposition, PlanDisposition


def test_tail_returns_full_text_when_under_limit():
    assert _tail("short error", limit=800) == "short error"


def test_tail_snaps_to_line_boundary_and_marks_truncation():
    # A long first line then a clean second line; the tail budget lands inside
    # the first line, so the excerpt must drop that partial line, not begin
    # mid-word, and must announce how much was dropped.
    text = (
        "verification failed: apdl-oss/sdk/dist/apdl.esm.js\n"
        + "x" * 50
        + "\nreal error line"
    )
    out = _tail(text, limit=20)
    assert out.startswith("[…truncated ")
    body = out.split("\n", 1)[1]
    # No partial leading line survived: the body starts at a real line boundary.
    assert "apdl-oss/sdk" not in body
    assert body.endswith("real error line")


def test_tail_strips_before_measuring():
    assert _tail("   hi   ", limit=800) == "hi"


def test_tail_omits_an_overlong_final_line_instead_of_slicing_it():
    out = _tail("earlier line\n" + ("x" * 1000), limit=80)

    assert "final line exceeds the 80-char excerpt limit" in out
    assert "x" * 10 not in out


def test_tail_keeps_a_complete_line_that_exactly_fits_the_limit():
    out = _tail("earlier line\n" + ("y" * 20), limit=20)

    assert out.split("\n", 1)[1] == "y" * 20


def test_parse_numstat_sums_files_and_lines():
    out = b"10\t2\tapp/a.py\x005\t0\tapp/b.py\x00"
    assert _parse_numstat(out) == (
        {"files": 2, "additions": 15, "deletions": 2},
        ["app/a.py", "app/b.py"],
    )


def test_parse_numstat_counts_binary_as_touched_zero_lines():
    out = b"-\t-\tassets/logo.png\x003\t1\tREADME.md\x00"
    assert _parse_numstat(out) == (
        {"files": 2, "additions": 3, "deletions": 1},
        ["assets/logo.png", "README.md"],
    )


def test_parse_numstat_rejects_malformed_records():
    with pytest.raises(ValueError, match="malformed"):
        _parse_numstat(b"garbage\x00")


def test_capability_preamble_warns_when_no_runner():
    text = _capability_preamble(has_test_runner=False, verify_cmd="npm run build")
    assert "NO test framework" in text
    assert "npm run build" in text
    assert (
        "do NOT import a test library" in text.lower()
        or "not import a test" in text.lower()
    )


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
    msg = _build_message(
        "Do the thing.", ["keep it small"], preamble="## Context\nrepo has no tests"
    )
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


def test_agent_env_forwards_llm_keys_but_not_github_or_apdl_secrets(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xyz")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-xyz")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----")
    monkeypatch.setenv("APDL_INTERNAL_TOKEN", "internal")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://nope")

    env = _agent_env(tmp_path / "agent-home")

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-xyz"
    assert env["OPENAI_API_KEY"] == "sk-openai-xyz"
    assert "GITHUB_APP_PRIVATE_KEY" not in env
    assert "APDL_INTERNAL_TOKEN" not in env
    assert "POSTGRES_URL" not in env
    assert env["HOME"] == str(tmp_path / "agent-home")
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["AIDER_CONFIG_FILE"] == "/dev/null"


@pytest.mark.asyncio
async def test_aider_argv_disables_repository_command_and_config_loading(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEGEN_KEEP_WORKDIR", "true")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    argv = await _capture_aider_argv(editor, monkeypatch)

    for flag in (
        "--no-auto-lint",
        "--no-auto-test",
        "--no-suggest-shell-commands",
        "--no-git-commit-verify",
        "--no-detect-urls",
        "--disable-playwright",
        "--no-analytics",
        "--no-check-update",
    ):
        assert flag in argv
    config_path = Path(argv[argv.index("--config") + 1])
    env_path = Path(argv[argv.index("--env-file") + 1])
    assert config_path.parent.name.startswith("apdl-cs-")
    assert "repo" not in config_path.parts
    assert config_path.read_text(encoding="utf-8") == "{}\n"
    assert env_path.read_text(encoding="utf-8") == ""


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

    async def fake_git(_cwd, args, **_kwargs):
        if args and args[0] == "clone":
            Path(args[-1]).mkdir(parents=True)
        return 0, ""  # clone / checkout / config all succeed

    async def fake_exec(argv, **_kwargs):
        captured["argv"] = argv
        return 1, "stop"  # non-zero bails right after the aider call

    monkeypatch.setattr(editor, "_git", fake_git)
    monkeypatch.setattr(editor, "_exec", fake_exec)

    await editor.implement(
        EditRequest(
            repo="acme/widgets",
            base_branch="main",
            branch="apdl/x",
            token="ghs_tok",
            title="x",
            spec="do a thing",
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
    read_names = [
        Path(argv[index + 1]).name
        for index, value in enumerate(argv)
        if value == "--read"
    ]
    assert "CONVENTIONS.md" not in read_names
    assert "INSPECTION.md" in read_names


def _fake_probe_with_refs(refs):
    """A probe with no verify context but the given SDK references."""
    return _RepoProbe(
        verify_cmd=None, has_test_runner=False, preamble="", sdk_references=refs
    )


@pytest.mark.asyncio
async def test_aider_argv_does_not_read_unversioned_sdk_reference(
    monkeypatch, tmp_path
):
    # Static SDK guidance is disabled unless it can be tied to an exact version.
    monkeypatch.setenv("CODEGEN_KEEP_WORKDIR", "true")
    monkeypatch.setattr(
        aider_editor,
        "_probe_repo",
        lambda *_a, **_k: _fake_probe_with_refs((("APDL_SDK_JS.md", "JS REF BODY"),)),
    )
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    argv = await _capture_aider_argv(editor, monkeypatch)

    read_paths = [Path(argv[i + 1]) for i, a in enumerate(argv) if a == "--read"]
    assert not any(p.name == "APDL_SDK_JS.md" for p in read_paths)


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
async def test_sdk_reference_env_cannot_enable_unversioned_guidance(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEGEN_SDK_REFERENCE", "true")
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
async def test_implement_does_not_run_local_verification_gate(monkeypatch, tmp_path):
    """GitHub CI owns verification; an absent local command is not a blocker."""
    monkeypatch.delenv("CODEGEN_REQUIRE_VERIFY", raising=False)  # default: on
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))

    async def fake_git(_cwd, args, **_kwargs):
        if args and args[0] == "clone":
            Path(args[-1]).mkdir(parents=True)
        return 0, ""  # clone / checkout / config succeed

    async def fake_exec(_argv, **_kwargs):
        return 0, "done"  # aider "succeeds" (empty clone → nothing to verify)

    async def fake_git_bytes(*_args, **_kwargs):
        return 0, b""

    monkeypatch.setattr(editor, "_git", fake_git)
    monkeypatch.setattr(editor, "_git_bytes", fake_git_bytes)
    monkeypatch.setattr(editor, "_exec", fake_exec)

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
    assert "unverified" not in (result.error or "")
    assert "no changes" in (result.error or "")


@pytest.mark.asyncio
async def test_implement_opts_out_of_verify_gate_when_disabled(monkeypatch, tmp_path):
    """CODEGEN_REQUIRE_VERIFY=false ⇒ the unverifiable-repo path no longer blocks.

    It fails later for a different reason (the faked clone has no diff), proving
    the fail-closed guard was bypassed rather than the run stopping at it.
    """
    monkeypatch.setenv("CODEGEN_REQUIRE_VERIFY", "false")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))

    async def fake_git(_cwd, args, **_kwargs):
        if args and args[0] == "clone":
            Path(args[-1]).mkdir(parents=True)
        return 0, ""

    async def fake_exec(_argv, **_kwargs):
        return 0, "done"

    async def fake_git_bytes(*_args, **_kwargs):
        return 0, b""

    monkeypatch.setattr(editor, "_git", fake_git)
    monkeypatch.setattr(editor, "_git_bytes", fake_git_bytes)
    monkeypatch.setattr(editor, "_exec", fake_exec)

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
    assert "unverified" not in (result.error or "")
    assert "no changes" in (result.error or "")


# --- The edit loop: brief → aider → verify → review, with feedback retries ----


def _request(test_cmd: str | None = "make test") -> EditRequest:
    return EditRequest(
        repo="acme/widgets",
        base_branch="main",
        branch="apdl/x",
        token="ghs_tok",
        title="Bot filter",
        spec="Build a bot filter.",
        test_cmd=test_cmd,
    )


def _enable_runtime_workflow(request: EditRequest) -> None:
    request.safety_policy = resolve_effective_policy(
        TenantCodegenConnectionPolicy.model_validate(
            {"runtime_acceptance": {"enabled": True}}
        ),
        PlatformCodegenSafetyPolicy(runtime_workflow_generation_enabled=True),
    )
    request.runtime_acceptance_policy = RuntimeAcceptancePolicy(
        enabled=request.safety_policy.runtime_workflow_generation_enabled
    )


class _Pipeline:
    """Scripted git/aider/test doubles that drive implement() end-to-end."""

    def __init__(
        self,
        editor,
        monkeypatch,
        *,
        test_results=None,
        diff_text=None,
        changed_paths: list[str] | None = None,
        repo_files: dict[str, str] | None = None,
        repo_symlinks: dict[str, Path] | None = None,
        edit_files: dict[str, str] | None = None,
    ):
        self.aider_messages: list[str] = []
        self.pushed = False
        self.git_calls: list[list[str]] = []
        self._test_results = list(test_results or [])
        self._diff_text = diff_text or "diff --git a/app/x.ts b/app/x.ts\n+new"
        self._changed_paths = list(changed_paths or ["app/x.ts\n"])
        self._last_changed_paths = ["app/x.ts"]
        self._head_reads = 0
        repo_path: Path | None = None

        async def fake_git(cwd, args, **_kwargs):
            nonlocal repo_path
            self.git_calls.append(list(args))
            if args[0] == "clone":
                repo = Path(args[-1])
                repo_path = repo
                repo.mkdir(parents=True)
                for relative, content in (repo_files or {}).items():
                    target = repo / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content)
                for relative, link_target in (repo_symlinks or {}).items():
                    target = repo / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(link_target)
            if args[0] == "diff":
                return 0, self._diff_text
            if args[:2] == ["rev-parse", "HEAD"]:
                self._head_reads += 1
                return 0, ("a" if self._head_reads == 1 else "c") * 40
            if args[:2] == ["rev-parse", "HEAD^{tree}"]:
                return 0, "b" * 40
            if args[0] == "rev-list":
                return 0, "abc123 parent1\n"  # single-parent commit
            if args[0] == "push":
                self.pushed = True
            return 0, ""  # clone / checkout / config / fetch / revert

        async def fake_git_bytes(_cwd, args, **_kwargs):
            self.git_calls.append(list(args))
            if args[0] == "diff" and "--name-only" in args:
                value = self._changed_paths[0]
                if len(self._changed_paths) > 1:
                    self._changed_paths.pop(0)
                self._last_changed_paths = [path for path in value.splitlines() if path]
                return (
                    0,
                    b"".join(
                        path.encode("utf-8") + b"\x00"
                        for path in self._last_changed_paths
                    ),
                )
            if args[0] == "diff" and "--numstat" in args:
                return (
                    0,
                    b"".join(
                        b"5\t1\t" + path.encode("utf-8") + b"\x00"
                        for path in self._last_changed_paths
                    ),
                )
            if args[0] == "diff" and "--binary" in args:
                return 0, self._diff_text.encode("utf-8")
            raise AssertionError(f"unexpected raw Git command: {args}")

        async def fake_exec(argv, **_kwargs):
            self.aider_messages.append(argv[argv.index("--message") + 1])
            assert repo_path is not None
            for relative, content in (edit_files or {}).items():
                target = repo_path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            return 0, "aider ok"

        monkeypatch.setattr(editor, "_git", fake_git)
        monkeypatch.setattr(editor, "_git_bytes", fake_git_bytes)
        monkeypatch.setattr(editor, "_exec", fake_exec)


def _strict_review_response(
    prompt: str,
    *,
    decision: str = "approved",
    rationale: str = "The repository evidence supports the change.",
    instructions: list[str] | None = None,
) -> str:
    requirement_ids = sorted(
        set(re.findall(r'"requirement_id": "(REQ-[0-9]{3})"', prompt))
    )
    evidence_ids = sorted(
        set(re.findall(r'"evidence_id": "(ev_[0-9a-f]{24})"', prompt))
    )
    assert requirement_ids and evidence_ids
    actions = instructions or (
        [] if decision == "approved" else ["Fix the evidenced defect."]
    )
    return json.dumps(
        {
            "schema_version": "review_model_response@1",
            "requirement_decisions": [
                {
                    "requirement_id": requirement_id,
                    "decision": decision,
                    "evidence_ids": [evidence_ids[0]],
                    "rationale": rationale,
                    "actionable_instructions": actions,
                }
                for requirement_id in requirement_ids
            ],
            "uncertainties": [],
            "actionable_instructions": actions,
        }
    )


def _routing_complete(brief_reply=None, review_replies=None):
    """One completer serving both auxiliary passes, routed by system prompt."""
    replies = list(review_replies or [])

    async def complete(system: str, user: str):
        if "engineering briefs" in system:
            return brief_reply
        reply = replies.pop(0) if replies else {"decision": "approved"}
        if isinstance(reply, str):
            return reply
        return _strict_review_response(user, **reply)

    return complete


_BRIEF = (
    "## Goal\nShip the bot filter.\n\n## Scope decisions\n- none\n\n"
    "## Implementation plan\n- edit app/x.ts\n\n## Acceptance criteria\n1. filter works"
) + "." * 200


@pytest.mark.asyncio
async def test_medium_risk_fails_closed_on_unusable_brief(monkeypatch, tmp_path):
    async def unavailable(_system, _user):
        return None

    editor = AiderEditor(
        model="claude-opus-4-8", workdir_base=str(tmp_path), complete=unavailable
    )
    pipeline = _Pipeline(editor, monkeypatch)
    request = _request()
    request.risk_level = "medium"

    result = await editor.implement(request)

    assert result.success is False
    assert "requires a parseable" in (result.error or "")
    assert pipeline.aider_messages == []


@pytest.mark.asyncio
async def test_high_risk_fails_closed_on_unparseable_review(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")

    async def unparseable(_system, _user):
        return "not json"

    editor = AiderEditor(
        model="claude-opus-4-8", workdir_base=str(tmp_path), complete=unparseable
    )
    pipeline = _Pipeline(editor, monkeypatch)
    request = _request()
    request.risk_level = "high"

    result = await editor.implement(request)

    assert result.success is False
    assert "semantic-review verdict" in (result.error or "")
    assert pipeline.pushed is False


@pytest.mark.asyncio
async def test_edit_loop_replaces_spec_with_compiled_brief(monkeypatch, tmp_path):
    editor = AiderEditor(
        model="claude-opus-4-8",
        workdir_base=str(tmp_path),
        complete=_routing_complete(brief_reply=_BRIEF),
    )
    pipeline = _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request())

    assert result.success is True
    assert pipeline.pushed is False
    assert result.base_sha == "a" * 40
    assert result.candidate_tree_sha == "b" * 40
    assert result.head_sha == "c" * 40
    assert base64.b64decode(result.patch_base64 or "", validate=True)
    assert result.inspection_snapshot is not None
    assert result.dependency_slice is not None
    assert [item.path for item in result.dependency_slice.changed_files] == ["app/x.ts"]
    assert len(pipeline.aider_messages) == 1
    assert "Ship the bot filter." in pipeline.aider_messages[0]
    # The prose work order replaces the raw spec as implementation guidance,
    # while the canonical ledger deliberately preserves the original source.
    assert "# Canonical requirement ledger" in pipeline.aider_messages[0]
    assert '"original_source_text": "Build a bot filter."' in pipeline.aider_messages[0]
    assert "# GitHub CI verification plan" in pipeline.aider_messages[0]
    assert "# GitHub CI Runtime Acceptance Plan" in pipeline.aider_messages[0]
    assert result.verification_plan is not None
    assert (
        result.verification_plan.disposition is PlanDisposition.unverified_external_ci
    )
    assert result.verification_coverage is not None
    assert (
        result.verification_coverage.disposition
        is CoverageDisposition.unverified_external_ci
    )
    assert result.runtime_acceptance_plan is not None


@pytest.mark.asyncio
async def test_authorized_runtime_workflow_is_generated_and_remains_policy_scoped(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    profile = RepoProfile(
        package_managers=[
            PackageManager(
                name="npm",
                manifest_path="package.json",
                lockfile_path="package-lock.json",
            )
        ],
        commands=[
            RepoCommand(
                kind=CommandKind.test,
                command="npm run test:old",
                cwd=".",
                source_path="package.json",
            )
        ],
        test_facilities=[
            ProfileTestFacility(
                name="vitest", package_path=".", source_path="package.json"
            )
        ],
    )
    monkeypatch.setattr(
        aider_editor,
        "_probe_repo",
        lambda *_a, **_k: _RepoProbe(
            verify_cmd="npm run test:old",
            has_test_runner=True,
            preamble="",
            profile=profile,
        ),
    )
    editor = AiderEditor(
        model="claude-opus-4-8",
        workdir_base=str(tmp_path),
        complete=_routing_complete(),
    )
    workflow = ".github/workflows/apdl-runtime-acceptance.yml"
    pipeline = _Pipeline(
        editor,
        monkeypatch,
        changed_paths=[f"app/x.ts\ntests/x.test.ts\n{workflow}\n"],
        repo_files={
            "package.json": json.dumps(
                {
                    "name": "runtime-demo",
                    "scripts": {"test:old": "vitest run"},
                    "devDependencies": {"vitest": "1.0.0"},
                }
            ),
            "package-lock.json": json.dumps(
                {"name": "runtime-demo", "lockfileVersion": 3, "packages": {}}
            ),
        },
        edit_files={
            "package.json": json.dumps(
                {
                    "name": "runtime-demo",
                    "scripts": {"test:new": "vitest run"},
                    "devDependencies": {"vitest": "1.0.0"},
                }
            )
        },
    )
    request = _request(test_cmd="npm run test:old")
    _enable_runtime_workflow(request)

    result = await editor.implement(request)

    assert result.success is True
    assert result.runtime_acceptance_plan is not None
    assert result.runtime_acceptance_plan.checks
    assert result.generated_runtime_workflow is not None
    assert result.runtime_acceptance_plan.generated_workflow is not None
    assert result.generated_runtime_workflow.path == workflow
    assert (
        result.generated_runtime_workflow.content_sha256
        == result.runtime_acceptance_plan.generated_workflow.content_sha256
    )
    assert (
        result.generated_runtime_workflow.runtime_acceptance_plan_sha256
        == result.runtime_acceptance_plan.evidence_hash()
    )
    assert {
        check.command.command for check in result.runtime_acceptance_plan.checks
    } == {"npm run test:new"}
    assert result.verification_coverage is not None
    assert result.verification_coverage.policy_authorized_workflow_paths == [workflow]
    assert result.verification_coverage.changed_protected_workflow_paths == []
    assert ["add", "--", workflow] in pipeline.git_calls
    assert (
        pipeline.git_calls.count(
            ["commit", "-m", "chore(ci): add runtime acceptance evidence"]
        )
        == 2
    )


@pytest.mark.asyncio
async def test_runtime_workflow_refuses_non_apdl_owned_path_collision(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    profile = RepoProfile(
        package_managers=[
            PackageManager(
                name="npm",
                manifest_path="package.json",
                lockfile_path="package-lock.json",
            )
        ],
        commands=[
            RepoCommand(
                kind=CommandKind.test,
                command="npm test",
                cwd=".",
                source_path="package.json",
            )
        ],
        test_facilities=[
            ProfileTestFacility(
                name="vitest", package_path=".", source_path="package.json"
            )
        ],
    )
    monkeypatch.setattr(
        aider_editor,
        "_probe_repo",
        lambda *_a, **_k: _RepoProbe(
            verify_cmd="npm test",
            has_test_runner=True,
            preamble="",
            profile=profile,
        ),
    )
    editor = AiderEditor(model="test", workdir_base=str(tmp_path))
    pipeline = _Pipeline(
        editor,
        monkeypatch,
        repo_files={
            "package.json": '{"scripts":{"test":"vitest run"}}',
            "package-lock.json": '{"lockfileVersion":3,"packages":{}}',
            RUNTIME_ACCEPTANCE_WORKFLOW_PATH: (
                "name: Existing repository workflow\non: [pull_request]\njobs: {}\n"
            ),
        },
    )
    request = _request(test_cmd="npm test")
    _enable_runtime_workflow(request)

    result = await editor.implement(request)

    assert result.success is False
    assert "already contains non-APDL-owned content" in (result.error or "")
    assert pipeline.aider_messages == []
    assert pipeline.pushed is False
    assert ["add", "--", RUNTIME_ACCEPTANCE_WORKFLOW_PATH] not in pipeline.git_calls


@pytest.mark.asyncio
async def test_runtime_workflow_refuses_symlink_to_outside_secret(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    profile = RepoProfile(
        package_managers=[
            PackageManager(
                name="npm",
                manifest_path="package.json",
                lockfile_path="package-lock.json",
            )
        ],
        commands=[
            RepoCommand(
                kind=CommandKind.test,
                command="npm test",
                cwd=".",
                source_path="package.json",
            )
        ],
        test_facilities=[
            ProfileTestFacility(
                name="vitest", package_path=".", source_path="package.json"
            )
        ],
    )
    monkeypatch.setattr(
        aider_editor,
        "_probe_repo",
        lambda *_a, **_k: _RepoProbe(
            verify_cmd="npm test",
            has_test_runner=True,
            preamble="",
            profile=profile,
        ),
    )
    outside = tmp_path / "outside-runtime-workflow.yml"
    outside.write_text(
        "OPENAI_API_KEY=provider-secret-that-must-not-be-read\n",
        encoding="utf-8",
    )
    editor = AiderEditor(model="test", workdir_base=str(tmp_path))
    pipeline = _Pipeline(
        editor,
        monkeypatch,
        repo_files={
            "package.json": '{"scripts":{"test":"vitest run"}}',
            "package-lock.json": '{"lockfileVersion":3,"packages":{}}',
        },
        repo_symlinks={RUNTIME_ACCEPTANCE_WORKFLOW_PATH: outside},
    )
    request = _request(test_cmd="npm test")
    _enable_runtime_workflow(request)

    result = await editor.implement(request)

    assert result.success is False
    assert "repository contains a symbolic link" in (result.error or "")
    assert "provider-secret-that-must-not-be-read" not in (result.error or "")
    assert pipeline.aider_messages == []
    assert pipeline.pushed is False


@pytest.mark.asyncio
async def test_generated_workflow_cannot_mask_an_agent_no_op(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    monkeypatch.setenv("CODEGEN_REVIEW", "false")
    profile = RepoProfile(
        package_managers=[
            PackageManager(
                name="npm",
                manifest_path="package.json",
                lockfile_path="package-lock.json",
            )
        ],
        commands=[
            RepoCommand(
                kind=CommandKind.test,
                command="npm test",
                cwd=".",
                source_path="package.json",
            )
        ],
        test_facilities=[
            ProfileTestFacility(
                name="vitest", package_path=".", source_path="package.json"
            )
        ],
    )
    monkeypatch.setattr(
        aider_editor,
        "_probe_repo",
        lambda *_a, **_k: _RepoProbe(
            verify_cmd="npm test",
            has_test_runner=True,
            preamble="",
            profile=profile,
        ),
    )
    editor = AiderEditor(model="test", workdir_base=str(tmp_path))
    workflow = ".github/workflows/apdl-runtime-acceptance.yml"
    pipeline = _Pipeline(
        editor,
        monkeypatch,
        changed_paths=[f"{workflow}\n"],
        repo_files={
            "package.json": '{"scripts":{"test":"vitest run"}}',
            "package-lock.json": '{"lockfileVersion":3,"packages":{}}',
        },
    )
    request = _request(test_cmd="npm test")
    _enable_runtime_workflow(request)

    result = await editor.implement(request)

    assert result.success is False
    assert "no material implementation change" in (result.error or "")
    assert pipeline.pushed is False


@pytest.mark.asyncio
async def test_medium_risk_missing_test_coverage_retries_before_push(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    monkeypatch.setenv("CODEGEN_EDIT_RETRIES", "1")
    profile = RepoProfile(
        commands=[
            RepoCommand(
                kind=CommandKind.test,
                command="pytest -q",
                cwd=".",
                source_path="pyproject.toml",
            )
        ],
        test_facilities=[
            ProfileTestFacility(
                name="pytest", package_path=".", source_path="pyproject.toml"
            )
        ],
        ci_workflows=[
            CIWorkflow(provider="github_actions", path=".github/workflows/ci.yml")
        ],
        protected_paths=[".github/workflows/ci.yml"],
    )
    monkeypatch.setattr(
        aider_editor,
        "_probe_repo",
        lambda *_a, **_k: _RepoProbe(
            verify_cmd="pytest -q",
            has_test_runner=True,
            preamble="",
            profile=profile,
        ),
    )
    editor = AiderEditor(
        model="claude-opus-4-8",
        workdir_base=str(tmp_path),
        complete=_routing_complete(),
    )
    pipeline = _Pipeline(
        editor,
        monkeypatch,
        changed_paths=["app/x.py\n", "app/x.py\ntests/test_x.py\n"],
    )
    request = _request(test_cmd="pytest -q")
    request.risk_level = "medium"

    result = await editor.implement(request)

    assert result.success is True
    assert pipeline.pushed is False
    assert len(pipeline.aider_messages) == 2
    assert "required verification coverage is missing" in pipeline.aider_messages[1]
    assert result.verification_coverage is not None
    assert (
        result.verification_coverage.disposition
        is CoverageDisposition.ready_for_github_ci
    )


@pytest.mark.asyncio
async def test_edit_loop_keeps_raw_spec_when_brief_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    editor = AiderEditor(
        model="claude-opus-4-8",
        workdir_base=str(tmp_path),
        complete=_routing_complete(),
    )
    pipeline = _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request())

    assert result.success is True
    assert "Build a bot filter." in pipeline.aider_messages[0]


@pytest.mark.asyncio
async def test_local_verify_failure_is_not_executed(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    monkeypatch.setenv("CODEGEN_REVIEW", "false")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    pipeline = _Pipeline(
        editor,
        monkeypatch,
        test_results=[(False, "Module not found: hashBucket"), (True, "")],
    )

    result = await editor.implement(_request())

    assert result.success is True
    assert len(pipeline.aider_messages) == 1


@pytest.mark.asyncio
async def test_local_verify_failures_do_not_block_generation(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    monkeypatch.setenv("CODEGEN_REVIEW", "false")
    monkeypatch.setenv("CODEGEN_EDIT_RETRIES", "1")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    pipeline = _Pipeline(
        editor, monkeypatch, test_results=[(False, "boom one"), (False, "boom two")]
    )

    result = await editor.implement(_request())

    assert result.success is True
    assert len(pipeline.aider_messages) == 1
    assert pipeline.pushed is False


@pytest.mark.asyncio
async def test_review_rejection_retries_with_instructions(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    editor = AiderEditor(
        model="claude-opus-4-8",
        workdir_base=str(tmp_path),
        complete=_routing_complete(
            review_replies=[
                {
                    "decision": "rejected",
                    "rationale": "The link targets a route that does not exist.",
                    "instructions": ["Create the page and wire it in."],
                },
                {"decision": "approved"},
            ]
        ),
    )
    pipeline = _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request())

    assert result.success is True
    assert len(pipeline.aider_messages) == 2
    retry = pipeline.aider_messages[1]
    assert "REJECTED" in retry
    assert "Create the page and wire it in." in retry


@pytest.mark.asyncio
async def test_review_rejection_without_retries_fails_the_changeset(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    monkeypatch.setenv("CODEGEN_EDIT_RETRIES", "0")
    editor = AiderEditor(
        model="claude-opus-4-8",
        workdir_base=str(tmp_path),
        complete=_routing_complete(
            review_replies=[
                {
                    "decision": "rejected",
                    "rationale": "The diff is only a token gesture.",
                    "instructions": ["Implement the complete behavior."],
                }
            ]
        ),
    )
    pipeline = _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request())

    assert result.success is False
    assert "semantic review rejected" in (result.error or "")
    assert "Implement the complete behavior" in (result.error or "")
    assert pipeline.pushed is False


@pytest.mark.asyncio
async def test_pipeline_runs_without_any_completer(monkeypatch, tmp_path):
    """No LiteLLM / no key ⇒ both auxiliary passes skip and the edit still ships."""
    monkeypatch.setattr(aider_editor, "resolve_completer", lambda: None)
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    pipeline = _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request())

    assert result.success is True
    assert pipeline.pushed is False
    assert "Build a bot filter." in pipeline.aider_messages[0]


# --- Pre-push gates run inside the editor, BEFORE anything reaches the remote --


@pytest.mark.asyncio
async def test_secret_in_diff_blocks_the_push_itself(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    monkeypatch.setenv("CODEGEN_REVIEW", "false")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    pipeline = _Pipeline(
        editor,
        monkeypatch,
        diff_text="diff --git a/.env.local b/.env.local\n+AWS_KEY=AKIAIOSFODNN7EXAMPLE",
    )

    result = await editor.implement(_request())

    assert result.success is False
    assert "gate" in (result.error or "").lower()
    # The branch never left the sandbox — this is the point of gating pre-push.
    assert pipeline.pushed is False


@pytest.mark.asyncio
async def test_gates_honor_the_connection_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    monkeypatch.setenv("CODEGEN_REVIEW", "false")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    pipeline = _Pipeline(editor, monkeypatch)  # numstat reports 6 changed lines

    request = _request()
    request.safety_policy = resolve_effective_policy(
        TenantCodegenConnectionPolicy(gates=TenantCodegenGatesPolicy(max_lines=5)),
        PlatformCodegenSafetyPolicy(),
    )
    result = await editor.implement(request)

    assert result.success is False
    assert "exceeding" in (result.error or "")
    assert pipeline.pushed is False


# --- Retry messages keep the original work order ------------------------------


@pytest.mark.asyncio
async def test_verification_context_stays_in_generation_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    monkeypatch.setenv("CODEGEN_REVIEW", "false")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    pipeline = _Pipeline(
        editor, monkeypatch, test_results=[(False, "boom"), (True, "")]
    )

    result = await editor.implement(_request())

    assert result.success is True
    message = pipeline.aider_messages[0]
    assert "Build a bot filter." in message
    assert "Repository verification context" in message
    assert "boom" not in message


@pytest.mark.asyncio
async def test_brief_message_does_not_duplicate_the_preamble(monkeypatch, tmp_path):
    # The brief is compiled WITH the verification context as input; prepending
    # it again would put the same block in the message twice.
    editor = AiderEditor(
        model="claude-opus-4-8",
        workdir_base=str(tmp_path),
        complete=_routing_complete(brief_reply=_BRIEF),
    )
    pipeline = _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request())

    assert result.success is True
    assert "Repository verification context" not in pipeline.aider_messages[0]


# --- Deterministic revert ------------------------------------------------------


def _revert_request() -> EditRequest:
    request = _request()
    request.revert_sha = "abc123"
    request.spec = "Revert pull request #7."
    return request


@pytest.mark.asyncio
async def test_revert_applies_git_revert_without_invoking_the_agent(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEGEN_REVIEW", "false")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    pipeline = _Pipeline(editor, monkeypatch)

    result = await editor.implement(_revert_request())

    assert result.success is True
    assert pipeline.pushed is False
    assert pipeline.aider_messages == []  # the revert is mechanical, not prose
    reverts = [c for c in pipeline.git_calls if c[0] == "revert"]
    assert reverts == [["revert", "--no-edit", "abc123"]]
    # The target commit was fetched into the shallow clone first.
    assert any(c[0] == "fetch" and "abc123" in c for c in pipeline.git_calls)


@pytest.mark.asyncio
async def test_revert_does_not_run_local_verification(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_REVIEW", "false")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    pipeline = _Pipeline(
        editor,
        monkeypatch,
        test_results=[(False, "type error after revert"), (True, "")],
    )

    result = await editor.implement(_revert_request())

    assert result.success is True
    assert len(pipeline.aider_messages) == 0


@pytest.mark.asyncio
async def test_revert_skips_the_quality_review(monkeypatch, tmp_path):
    # A mechanical revert's diff is not judged against the spec — a reviewer
    # rejection here would be noise, not signal.
    rejected = (
        '{"approved": false, "problems": ["looks weird"], "fix_instructions": "x"}'
    )
    editor = AiderEditor(
        model="claude-opus-4-8",
        workdir_base=str(tmp_path),
        complete=_routing_complete(review_replies=[rejected]),
    )
    pipeline = _Pipeline(editor, monkeypatch)

    result = await editor.implement(_revert_request())

    assert result.success is True
    assert pipeline.pushed is False


@pytest.mark.asyncio
async def test_revert_conflict_fails_cleanly(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_REVIEW", "false")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    pipeline = _Pipeline(editor, monkeypatch)

    async def conflicting_git(cwd, args, **_kwargs):
        pipeline.git_calls.append(list(args))
        if args[0] == "clone":
            Path(args[-1]).mkdir(parents=True)
        if args[0] == "rev-list":
            return 0, "abc123 parent1\n"
        if args[0] == "revert" and "--abort" not in args:
            return 1, "error: could not revert abc123"
        return 0, ""

    monkeypatch.setattr(editor, "_git", conflicting_git)

    result = await editor.implement(_revert_request())

    assert result.success is False
    assert "conflicts" in (result.error or "")
    assert pipeline.pushed is False
    # The conflicted revert was aborted, not left half-applied.
    assert ["revert", "--abort"] in pipeline.git_calls


# --- The prompt transcript (EditResult.prompts → admin console) ---------------


@pytest.mark.asyncio
async def test_prompt_transcript_records_brief_edit_and_review(monkeypatch, tmp_path):
    editor = AiderEditor(
        model="claude-opus-4-8",
        workdir_base=str(tmp_path),
        complete=_routing_complete(brief_reply=_BRIEF),
    )
    _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request())

    assert result.success is True
    assert [p["stage"] for p in result.prompts] == ["brief", "edit", "review"]
    brief, edit, review = result.prompts

    assert brief["system"].startswith("You compile approved product feature proposals")
    assert "Build a bot filter." in brief["user"]  # the raw spec
    assert "# Repository digest" in brief["user"]
    assert brief["notes"] is None

    # The agent's message carries the compiled brief; the transcript is honest
    # that the system prompt at this step is Aider's own, not APDL-authored.
    assert edit["system"] is None
    assert "Ship the bot filter." in edit["user"]
    assert "Aider's built-in editing prompt" in edit["notes"]

    assert review["system"].startswith("You review an automated code change")
    assert '"original_source_text": "Build a bot filter."' in review["user"]
    assert "```diff" in review["user"]


@pytest.mark.asyncio
async def test_prompt_transcript_notes_brief_fallback(monkeypatch, tmp_path):
    """An unusable brief still leaves its prompt recorded, with the fallback noted."""
    monkeypatch.setenv("CODEGEN_REVIEW", "false")
    events: list[str] = []
    real_append_prompt = aider_editor.append_prompt

    def recording_append_prompt(transcript, prompt):
        events.append(f"append:{prompt['stage']}")
        real_append_prompt(transcript, prompt)

    async def unusable_brief(_system, _user):
        events.append("brief-call")
        return "too short"

    monkeypatch.setattr(aider_editor, "append_prompt", recording_append_prompt)
    editor = AiderEditor(
        model="claude-opus-4-8",
        workdir_base=str(tmp_path),
        complete=unusable_brief,
    )
    _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request())

    assert result.success is True
    brief = result.prompts[0]
    assert brief["stage"] == "brief"
    assert "no usable brief" in brief["notes"]
    assert events[:2] == ["append:brief", "brief-call"]
    # The edit ran on the raw spec.
    assert "Build a bot filter." in result.prompts[1]["user"]


@pytest.mark.asyncio
async def test_prompt_transcript_survives_raised_brief_completion(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEGEN_REVIEW", "false")

    async def broken_brief(_system, _user):
        raise RuntimeError("provider unavailable")

    editor = AiderEditor(
        model="claude-opus-4-8",
        workdir_base=str(tmp_path),
        complete=broken_brief,
    )
    _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request())

    assert result.success is True
    assert [prompt["stage"] for prompt in result.prompts] == ["brief", "edit"]
    assert "failed (RuntimeError)" in result.prompts[0]["notes"]


@pytest.mark.asyncio
async def test_prompt_transcript_has_no_local_verify_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    monkeypatch.setenv("CODEGEN_REVIEW", "false")
    monkeypatch.setenv("CODEGEN_EDIT_RETRIES", "1")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    _Pipeline(
        editor,
        monkeypatch,
        test_results=[(False, "Module not found: hashBucket"), (False, "still red")],
    )

    result = await editor.implement(_request())

    assert result.success is True
    labels = [p["label"] for p in result.prompts]
    assert labels == ["Edit instruction (attempt 1)"]


@pytest.mark.asyncio
async def test_prompt_transcript_lists_attached_context_files(monkeypatch, tmp_path):
    monkeypatch.setattr(
        aider_editor,
        "_probe_repo",
        lambda *_a, **_k: _fake_probe_with_refs((("APDL_SDK_JS.md", "JS REF BODY"),)),
    )
    monkeypatch.setenv("CODEGEN_BRIEF", "false")
    monkeypatch.setenv("CODEGEN_REVIEW", "false")
    editor = AiderEditor(model="claude-opus-4-8", workdir_base=str(tmp_path))
    _Pipeline(editor, monkeypatch)

    result = await editor.implement(_request(test_cmd=None))

    edit = next(p for p in result.prompts if p["stage"] == "edit")
    assert "CONVENTIONS.md" in edit["notes"]
    assert "INSPECTION.md" in edit["notes"]
    assert "APDL_SDK_JS.md" not in edit["notes"]


@pytest.mark.asyncio
async def test_named_dependency_blocks_when_install_is_not_isolated(
    monkeypatch, tmp_path
):
    """Exact package claims cannot fall back to model knowledge on the API host."""
    profile = RepoProfile(
        packages=[
            PackageBoundary(
                path=".", ecosystem="node", name="demo", manifest_path="package.json"
            )
        ],
        package_managers=[
            PackageManager(
                name="npm",
                manifest_path="package.json",
                lockfile_path="package-lock.json",
            )
        ],
        lockfiles=["package-lock.json"],
        dependencies=[
            Dependency(
                name="exact-sdk",
                ecosystem="node",
                package_path=".",
                declared_constraint="^1.0.0",
                resolved_version="1.2.3",
                source_path="package-lock.json",
            )
        ],
    )
    monkeypatch.setattr(
        aider_editor,
        "_probe_repo",
        lambda *_a, **_k: _RepoProbe(
            verify_cmd=None,
            has_test_runner=False,
            preamble="",
            profile=profile,
        ),
    )
    editor = AiderEditor(model="test", workdir_base=str(tmp_path))

    async def fake_git(_cwd, args, **_kwargs):
        if args and args[0] == "clone":
            repo = Path(args[-1])
            repo.mkdir(parents=True)
            (repo / "package.json").write_text(
                '{"name":"demo","dependencies":{"exact-sdk":"^1.0.0"}}'
            )
            (repo / "package-lock.json").write_text('{"lockfileVersion":3}')
        if args[:2] == ["rev-parse", "HEAD"]:
            return 0, "base-sha"
        return 0, ""

    async def unexpected_exec(*_args, **_kwargs):
        raise AssertionError("the editing model must not run without exact contracts")

    monkeypatch.setattr(editor, "_git", fake_git)
    monkeypatch.setattr(editor, "_exec", unexpected_exec)

    result = await editor.implement(
        EditRequest(
            repo="acme/demo",
            project_scope="project-1",
            base_branch="main",
            branch="apdl/exact",
            token="token",
            title="Use exact-sdk",
            spec="Call exact-sdk from the existing handler.",
        )
    )

    assert result.success is False
    assert "refused outside an explicit sandbox" in (result.error or "")
    assert result.contract_bundle is not None
    assert result.contract_bundle.resolutions[0].disposition == "blocked"
