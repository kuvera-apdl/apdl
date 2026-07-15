"""Aider-backed Editor (plan decision D3, reworked — model-agnostic OSS agent).

Replaces the Claude Managed Agents editor. We now run the edit loop ourselves
with `Aider <https://github.com/Aider-AI/aider>`_, a git-native, model-agnostic
coding agent: it reaches any LiteLLM-supported model (OpenAI, Anthropic, Google,
local, …) chosen via ``CODEGEN_MODEL``, edits inside a clone, and iterates on test
failures with ``--auto-test``. The model is now a config choice, not a vendor
lock-in.

Execution model (v1): a subprocess in a constrained, throwaway workdir on the
codegen host. The hardened container image (``Dockerfile.worker``) is the deploy
substrate to graduate to — running each changeset inside its own container is the
documented next step. This real path is INTEGRATION-UNTESTED here (it needs
``aider`` on PATH, a model key, and a live repo) — the tested execution path runs
through ``FakeEditor``.

Token custody (entirely ours now — there is no Anthropic git proxy): the GitHub
installation token is used only by the orchestrator for clone/push, injected into
git as a one-shot ``http.extraHeader`` via ``GIT_CONFIG_*`` environment variables.
Going through the env (not ``-c`` on the command line) keeps the header — and the
reversible base64 token inside it — off git's argv, so it can't leak through
``ps`` / ``/proc/<pid>/cmdline``; it is likewise never written to ``.git/config``.
It is never handed to the agent/test subprocesses either: the agent runs with a
minimal env — PATH/HOME plus LLM provider keys only — never the GitHub token or
APDL secrets.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import config
from app.editor.base import EditRequest, EditResult
from app.editor.brief import (
    BRIEF_SYSTEM,
    build_brief_user,
    build_repo_digest,
    compile_brief,
)
from app.editor.conventions import CONVENTIONS_MD
from app.editor.llm import CompleteFn, resolve_completer
from app.editor.review import (
    REVIEW_SYSTEM,
    ReviewVerdict,
    build_review_user,
    review_change,
)
from app.editor.sdk_reference import detect_sdk_references
from app.safety.gates import evaluate_pre_push

logger = logging.getLogger(__name__)

_DIFF_TEXT_CAP = 1_000_000  # cap the diff text fed to the secret scan (chars)
_ERR_TAIL = 800  # how much subprocess output to surface on a generic failure
# Verification/test failures are the actionable ``tests_failed`` case an operator
# reads to fix the change, so they get a much larger budget: a build/test log's
# real error (failing file, import, assertion, stack) needs room to survive.
_VERIFY_ERR_TAIL = 6000


def _tail(text: str, limit: int = _ERR_TAIL) -> str:
    """Return the last ``limit`` chars of ``text`` as a clean failure excerpt.

    Two fixes over a bare ``text[-limit:]`` so a surfaced error is actually
    informative: (1) the slice is snapped to the next line boundary so the
    excerpt never begins mid-line (which reads as corrupted — e.g. an import
    path shown as ``s/sdk/dist/...``), and (2) when content is dropped a marker
    naming how much was truncated is prepended, so a reader knows the head of the
    log is missing rather than assuming they have the whole story.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    clipped = text[-limit:]
    # Drop the partial leading line so the excerpt starts on a clean boundary.
    newline = clipped.find("\n")
    if 0 <= newline < len(clipped) - 1:
        clipped = clipped[newline + 1 :]
    dropped = len(text) - len(clipped)
    return f"[…truncated {dropped} leading chars of {len(text)}…]\n{clipped}"

# Env vars forwarded to the agent + test subprocesses. LLM access only: the
# GitHub installation token and APDL service secrets (GITHUB_APP_PRIVATE_KEY,
# APDL_INTERNAL_TOKEN, POSTGRES_URL, …) are deliberately NOT in this allowlist.
_ENV_PASSTHROUGH: tuple[str, ...] = (
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR",
    "OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "VERTEXAI_PROJECT", "VERTEXAI_LOCATION",
    "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY",
    "COHERE_API_KEY", "TOGETHERAI_API_KEY", "FIREWORKS_API_KEY", "XAI_API_KEY",
    "OLLAMA_API_BASE", "AZURE_API_KEY", "AZURE_API_BASE", "AZURE_API_VERSION",
)

#: Best-effort test-command detection when the connection policy doesn't set one.
#: package.json is handled separately (see ``_npm_test_cmd``) because it needs a
#: dependency install and a scripts lookup, not a fixed command.
_TEST_DETECTORS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "python -m pytest -q"),
    ("pytest.ini", "python -m pytest -q"),
    ("tox.ini", "python -m pytest -q"),
    ("go.mod", "go test ./..."),
    ("Cargo.toml", "cargo test"),
)

#: npm needs deps before any script runs; a fresh clone has no node_modules.
_NPM_INSTALL = "npm install --no-audit --no-fund --silent"

#: JS/TS test runners that count as "the repo has a test framework" even when no
#: ``test`` script is wired up. Used only to shape the agent's guidance (whether
#: it may add tests), never to run anything.
_JS_TEST_RUNNERS: tuple[str, ...] = (
    "vitest", "jest", "mocha", "ava", "@playwright/test", "cypress", "node:test",
)


def _basic_auth_header(token: str) -> str:
    """GitHub App token → a one-shot Basic auth header value (never persisted)."""
    raw = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"AUTHORIZATION: basic {raw}"


def _agent_env() -> dict[str, str]:
    """Minimal environment for the agent subprocess — LLM keys only."""
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env.setdefault("PATH", os.defpath)
    env["AIDER_ANALYTICS"] = "false"  # headless: no phone-home / update prompts
    env["AIDER_CHECK_UPDATE"] = "false"
    return env


#: Env vars for the repo's own build/test subprocess. NO provider keys: the test
#: command executes untrusted repo code (npm postinstall scripts, test files),
#: which must never see the LLM API keys. Aider necessarily runs its test loop
#: with its own env, but the independent verify run has no reason to.
_TEST_ENV_PASSTHROUGH: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


def _test_env() -> dict[str, str]:
    """Stripped environment for running the repo's verification command."""
    env = {k: os.environ[k] for k in _TEST_ENV_PASSTHROUGH if k in os.environ}
    env.setdefault("PATH", os.defpath)
    return env


def _git_env() -> dict[str, str]:
    """Git environment: no credential prompts, no inherited app/LLM secrets."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    env.setdefault("PATH", os.defpath)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _parse_numstat(numstat: str) -> dict[str, int]:
    """Parse ``git diff --numstat`` into a diff_stat dict.

    Returns ``{"files", "additions", "deletions"}``. Binary files render their
    counts as ``-``; they count toward ``files`` but contribute zero lines.
    """
    files = additions = deletions = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, _path = parts
        files += 1
        additions += int(added) if added.isdigit() else 0
        deletions += int(removed) if removed.isdigit() else 0
    return {"files": files, "additions": additions, "deletions": deletions}


def _npm_scripts(package_json: Path) -> dict:
    """Return a repo's ``package.json`` ``scripts`` map (empty on any error)."""
    try:
        scripts = json.loads(
            package_json.read_text(encoding="utf-8", errors="ignore")
        ).get("scripts", {})
    except (OSError, ValueError):
        scripts = {}
    return scripts if isinstance(scripts, dict) else {}


def _npm_verify_cmd(package_json: Path) -> str | None:
    """Compose an npm verification command: install, then a type/build gate, then tests.

    A freshly cloned repo has no ``node_modules``, so we install first. We ALWAYS
    run a type/build gate for a JS/TS repo — a ``test`` script alone does not
    type-check the whole project, and ``next build`` / ``tsc --noEmit`` reject an
    unresolved import (e.g. a test file importing a runner the repo never
    installed) that ``eslint`` and unit tests happily ignore. That gap is exactly
    how a build-breaking PR ships past a "tests passed" check. Tests run last when
    a ``test`` script exists. Returns ``None`` only when there is genuinely nothing
    to verify (no build, no tsconfig, no tests) — the caller decides whether that
    blocks the PR (see ``codegen_require_verify``).
    """
    scripts = _npm_scripts(package_json)
    steps = [_NPM_INSTALL]

    # Type/build gate: prefer the repo's own build; else a bare tsc --noEmit when
    # the repo is TypeScript. A JS-only repo with no build has no type gate.
    if scripts.get("build"):
        steps.append("npm run build")
    elif (package_json.parent / "tsconfig.json").is_file():
        steps.append("npx --no-install tsc --noEmit")

    if scripts.get("test"):
        steps.append("npm test --silent")

    if len(steps) == 1:  # install only → nothing meaningful to verify
        logger.warning(
            "package.json in %s has no build/tsconfig/test to verify against.",
            package_json.parent,
        )
        return None
    return " && ".join(steps)


def _makefile_has_test(repo_dir: Path) -> bool:
    """True when the repo's ``Makefile`` declares a ``test:`` target."""
    makefile = repo_dir / "Makefile"
    if not makefile.is_file():
        return False
    try:
        text = makefile.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(line.startswith("test:") for line in text.splitlines())


def _detect_test_cmd(repo_dir: Path) -> str | None:
    """Best-effort repo verification command when the connection policy sets none.

    For a JS/TS repo this is a chained install + type/build gate + tests (see
    ``_npm_verify_cmd``); for other ecosystems it is the native test command.
    """
    if _makefile_has_test(repo_dir):
        return "make test"
    package_json = repo_dir / "package.json"
    if package_json.is_file():
        return _npm_verify_cmd(package_json)
    for filename, cmd in _TEST_DETECTORS:
        if (repo_dir / filename).is_file():
            return cmd
    return None


def _repo_has_test_runner(repo_dir: Path) -> bool:
    """Whether the repo already has a test framework the agent may write tests in.

    Shapes the agent's guidance only (never runs anything). For JS/TS: a ``test``
    script or a known runner in the manifest. For other ecosystems: presence of a
    pytest/go/cargo config or a Makefile ``test`` target — those detectors are
    real test commands, so a runner exists.
    """
    package_json = repo_dir / "package.json"
    if package_json.is_file():
        if _npm_scripts(package_json).get("test"):
            return True
        try:
            data = json.loads(
                package_json.read_text(encoding="utf-8", errors="ignore")
            )
        except (OSError, ValueError):
            return False
        deps: dict = {}
        for key in ("dependencies", "devDependencies"):
            section = data.get(key)
            if isinstance(section, dict):
                deps.update(section)
        return any(runner in deps for runner in _JS_TEST_RUNNERS)
    if _makefile_has_test(repo_dir):
        return True
    return any((repo_dir / filename).is_file() for filename, _ in _TEST_DETECTORS)


def _capability_preamble(has_test_runner: bool, verify_cmd: str | None) -> str:
    """The per-repo 'testing reality' block prepended to the agent's message.

    Grounds the agent in what this specific repo can run, so it neither fabricates
    a test framework the repo lacks (which breaks the build) nor skips tests where
    a runner exists.
    """
    lines = ["## Repository verification context (read before writing code)"]
    if verify_cmd:
        lines.append(
            f"Your change is gated on this command passing: `{verify_cmd}`. It "
            "runs a type/build check and any tests; a change that fails it is "
            "rejected, not merged. Make sure everything you add passes it."
        )
    else:
        lines.append(
            "No automated verification command was detected for this repo. Keep "
            "the change minimal and self-contained."
        )
    if has_test_runner:
        lines.append(
            "This repo HAS a test framework. Add a test that exercises the new "
            "behavior, using the framework the repo ALREADY depends on — never a "
            "different one."
        )
    else:
        lines.append(
            "This repo has NO test framework configured. Do NOT add test files and "
            "do NOT import a test library (vitest/jest/pytest/…): the dependency is "
            "absent, so the import will fail the build/type-check. Rely on the "
            "verification command above. Only add a runner if it is essential to "
            "the feature, and then add it to the manifest + lockfile in this change."
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class _RepoProbe:
    """What the sandbox learned about a cloned repo before invoking the agent."""

    verify_cmd: str | None
    has_test_runner: bool
    preamble: str
    #: ``(filename, markdown)`` SDK references the repo's manifests call for.
    sdk_references: tuple[tuple[str, str], ...] = ()


def _probe_repo(repo_dir: Path, override_cmd: str | None) -> _RepoProbe:
    """Resolve the verification command + agent guidance for a cloned repo.

    ``override_cmd`` (the connection policy's ``test_cmd``) wins as the gate when
    set; the runner-presence signal still comes from the repo so the agent's
    guidance stays accurate.
    """
    verify_cmd = override_cmd or _detect_test_cmd(repo_dir)
    has_runner = _repo_has_test_runner(repo_dir)
    return _RepoProbe(
        verify_cmd=verify_cmd,
        has_test_runner=has_runner,
        preamble=_capability_preamble(has_runner, verify_cmd),
        sdk_references=tuple(detect_sdk_references(repo_dir)),
    )


def _build_message(spec: str, constraints: list[str], preamble: str = "") -> str:
    """Compose the single headless instruction handed to Aider.

    ``preamble`` (the per-repo verification context) leads the message so the
    agent reads the repo's testing reality before the task itself.
    """
    message = spec.strip()
    if constraints:
        bullets = "\n".join(f"- {c}" for c in constraints)
        message = f"{message}\n\nConstraints:\n{bullets}"
    if preamble.strip():
        message = f"{preamble.strip()}\n\n{message}"
    return message


def _with_feedback(base_message: str, feedback: str) -> str:
    """Compose a retry message: the ORIGINAL work order plus the failure feedback.

    Each aider invocation is a fresh process with no chat history, so a retry
    that carried only the failure text would strip the agent of the task, the
    constraints, and the repo's testing reality — exactly the round most likely
    to do something desperate without them.
    """
    return f"{base_message}\n\n# Previous attempt — feedback to address first\n\n{feedback}"


def _verify_retry_message(test_cmd: str, output: str) -> str:
    """Follow-up agent feedback after the post-edit verification failed.

    The agent's commits are already in the clone, so a follow-up invocation sees
    its own work; the message carries the failing output so the fix is informed,
    and pins the intent so the "fix" is a repair, not a revert.
    """
    return (
        "Your previous change is committed in this repository but FAILED the "
        f"verification command: `{test_cmd}`.\n\n"
        f"Failing output (tail):\n```\n{_tail(output, _VERIFY_ERR_TAIL)}\n```\n\n"
        "Fix the failure while keeping the implemented feature intact — repair "
        "the code; do not revert the work to make the command pass."
    )


def _review_retry_message(verdict: ReviewVerdict) -> str:
    """Follow-up agent feedback after the pre-push quality review rejected the diff."""
    problems = "\n".join(f"- {p}" for p in verdict.problems)
    instructions = verdict.fix_instructions.strip() or "Address every problem above."
    return (
        "An automated reviewer compared your committed change against the task "
        "spec and REJECTED it.\n\n"
        f"Problems found:\n{problems or '- (see instructions below)'}\n\n"
        f"Do this now:\n{instructions}"
    )


def _model_settings_yaml(model: str) -> str:
    """Aider model-settings that disable ``temperature``.

    Newer models (e.g. ``claude-opus-4-8``) reject the ``temperature`` parameter,
    but aider sends it by default — which silently fails the request and produces
    a no-op edit. Disabling it is safe (the model uses its own default).
    """
    return f'- name: "{model}"\n  use_temperature: false\n'


class AiderEditor:
    """Editor that drives Aider headlessly in a sandboxed clone (model-agnostic).

    The model is read from ``CODEGEN_MODEL`` (default ``claude-opus-4-8``); any
    LiteLLM-supported id works as long as the matching provider key is present in
    the service env (it is forwarded to the agent process, the GitHub token is
    not).
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        aider_bin: str | None = None,
        workdir_base: str | None = None,
        complete: CompleteFn | None = None,
    ) -> None:
        self._model = model or config.codegen_model()
        self._aider_bin = aider_bin or config.codegen_aider_bin()
        self._cache_prompts = config.codegen_cache_prompts()
        self._conventions = config.codegen_conventions_enabled()
        self._sdk_reference = config.codegen_sdk_reference_enabled()
        self._workdir_base = workdir_base or config.codegen_workdir()
        self._git_timeout = config.codegen_git_timeout()
        self._agent_timeout = config.codegen_agent_timeout()
        self._test_timeout = config.codegen_test_timeout()
        self._require_verify = config.codegen_require_verify()
        # Auxiliary LLM passes around the edit (brief compile + diff review).
        # ``complete`` is the injection seam for tests; production resolves a
        # LiteLLM-backed completer per run (None → the passes are skipped).
        self._complete = complete
        self._brief_enabled = config.codegen_brief_enabled()
        self._review_enabled = config.codegen_review_enabled()
        self._edit_retries = config.codegen_edit_retries()

    async def implement(self, request: EditRequest) -> EditResult:
        try:
            return await self._run(request)
        except Exception as exc:  # an attempt must never raise to the job runner
            logger.exception("Aider edit failed for %s", request.repo)
            return EditResult(success=False, branch=request.branch, error=str(exc))

    async def _run(self, request: EditRequest) -> EditResult:
        keep = config.codegen_keep_workdir()
        work = Path(tempfile.mkdtemp(prefix="apdl-cs-", dir=self._workdir_base))
        repo_dir = work / "repo"
        header = _basic_auth_header(request.token)
        clone_url = f"https://github.com/{request.repo}.git"
        # Prompt transcript for the operator UI (see EditResult.prompts). Every
        # exit path — fail() or success — carries whatever was recorded so far.
        prompts: list[dict[str, Any]] = []

        def fail(error: str) -> EditResult:
            return EditResult(
                success=False, branch=request.branch, error=error, prompts=prompts
            )

        try:
            # 1. Clone the base branch with a one-shot auth header (the token is
            #    NOT persisted to .git/config), then cut the work branch.
            rc, out = await self._git(
                None,
                ["clone", "--depth", "1",
                 "--branch", request.base_branch, clone_url, str(repo_dir)],
                auth_header=header,
            )
            if rc != 0:
                return fail(f"clone failed: {_tail(out)}")
            rc, out = await self._git(repo_dir, ["checkout", "-b", request.branch])
            if rc != 0:
                return fail(f"branch failed: {_tail(out)}")
            # Local commit identity so Aider's commits succeed without a global config.
            await self._git(repo_dir, ["config", "user.email", "codegen@apdl.dev"])
            await self._git(repo_dir, ["config", "user.name", "APDL Codegen"])

            # 2. Probe the repo: resolve the verification command (connection
            #    policy first, then detect) and the agent-facing testing reality.
            probe = _probe_repo(repo_dir, request.test_cmd)
            test_cmd = probe.verify_cmd

            # 3. Compile the spec into a repo-grounded engineering brief
            #    (auxiliary LLM pass; fail-open — an unusable brief means the raw
            #    spec runs, which is what would have happened anyway). The brief
            #    replaces the spec in the agent's message; the ORIGINAL spec
            #    stays the contract the post-edit review judges against. A
            #    deterministic revert needs no brief — the change is mechanical.
            need_brief = self._brief_enabled and request.revert_sha is None
            need_review = self._review_enabled and request.revert_sha is None
            complete = self._complete
            if complete is None and (need_brief or need_review):
                complete = resolve_completer()
            task_text = request.spec
            brief_used = False
            if need_brief and complete is not None:
                repo_digest = build_repo_digest(repo_dir)
                brief_prompt = {
                    "stage": "brief",
                    "label": "Brief compilation (spec → engineering brief)",
                    "system": BRIEF_SYSTEM,
                    "user": build_brief_user(
                        title=request.title,
                        spec=request.spec,
                        repo_digest=repo_digest,
                        verification_context=probe.preamble,
                    ),
                    "notes": None,
                }
                prompts.append(brief_prompt)
                brief = await compile_brief(
                    title=request.title,
                    spec=request.spec,
                    repo_digest=repo_digest,
                    verification_context=probe.preamble,
                    complete=complete,
                )
                if brief:
                    task_text = brief
                    brief_used = True
                else:
                    brief_prompt["notes"] = (
                        "Compilation produced no usable brief; the raw spec was "
                        "handed to the editing agent instead."
                    )

            # 4. Run Aider headless. It edits + commits locally; it does NOT push.
            #    The settings file lives outside repo_dir so it never enters the diff.
            settings_file = work / "aider.model.settings.yml"
            settings_file.write_text(_model_settings_yaml(self._model), encoding="utf-8")
            argv = [self._aider_bin, "--model", self._model,
                    "--model-settings-file", str(settings_file),
                    "--yes-always", "--no-stream", "--no-pretty"]
            if self._conventions:
                # Standing house rules as a read-only context file (kept outside
                # repo_dir so it never enters the diff). It joins the cacheable
                # static prefix, so --cache-prompts re-reads it at ~0.1x on each
                # auto-test retry instead of bloating the per-task message.
                conventions_file = work / "CONVENTIONS.md"
                conventions_file.write_text(CONVENTIONS_MD, encoding="utf-8")
                argv += ["--read", str(conventions_file)]
            if self._sdk_reference:
                # Language-scoped SDK call-path reference(s) for whichever APDL
                # SDK the repo depends on. Written outside repo_dir so they never
                # enter the diff; they give the agent the real track()/identify()
                # path the SDK (in node_modules / site-packages) hides from the
                # repo map. Only refs for an SDK actually present are attached.
                for ref_name, ref_body in probe.sdk_references:
                    ref_file = work / ref_name
                    ref_file.write_text(ref_body, encoding="utf-8")
                    argv += ["--read", str(ref_file)]
            if self._cache_prompts:
                # Cache the static prefix (system + repo map) so the auto-test
                # retry loop re-reads it at ~0.1x instead of full input price.
                argv.append("--cache-prompts")
            if test_cmd:
                argv += ["--auto-test", "--test-cmd", test_cmd]

            # 4b. A revert changeset is applied deterministically with
            #     ``git revert`` — the agent cannot see the merged commits (the
            #     clone is shallow) and must not reconstruct the revert from
            #     prose. The agent only steps in afterwards, if verification
            #     fails on the reverted tree.
            agent_pending = True
            if request.revert_sha:
                revert_error = await self._revert_commit(
                    repo_dir, request.revert_sha, header
                )
                if revert_error:
                    return fail(revert_error)
                agent_pending = False

            # 5. The edit loop: aider → verify → review, with a bounded number of
            #    feedback rounds. A verification or review failure re-invokes the
            #    agent with the failure in hand (its commits are already in the
            #    clone) instead of terminally failing the changeset — most
            #    first-round failures are fixable with the error visible. The
            #    brief already embeds the verification context, so it is only
            #    prepended when the raw spec runs.
            initial_message = _build_message(
                task_text, request.constraints, "" if brief_used else probe.preamble
            )
            # What the agent reads besides the message, for the transcript: its
            # own built-in system prompt plus any --read context files above.
            context_files = [Path(argv[i + 1]).name
                             for i, a in enumerate(argv) if a == "--read"]
            edit_notes = (
                "The system prompt for this step is Aider's built-in editing "
                "prompt (not authored by APDL)."
            )
            if context_files:
                edit_notes += (
                    " Read-only context files attached: "
                    + ", ".join(context_files) + "."
                )
            message = initial_message
            retries_left = self._edit_retries
            base = request.base_branch
            out = ""
            edit_attempt = 0
            review_round = 0
            while True:
                if agent_pending:
                    edit_attempt += 1
                    prompts.append({
                        "stage": "edit",
                        "label": f"Edit instruction (attempt {edit_attempt})",
                        "system": None,
                        "user": message,
                        "notes": edit_notes,
                    })
                    rc, out = await self._exec(
                        [*argv, "--message", message],
                        cwd=repo_dir, env=_agent_env(), timeout=self._agent_timeout,
                    )
                    if rc != 0:
                        return fail(f"aider exited {rc}: {_tail(out, _VERIFY_ERR_TAIL)}")
                agent_pending = True

                # Verify the change is actually green, outside the agent's
                # control. Fail closed: a change we could not verify does not get
                # a PR unless an operator has explicitly opted out
                # (CODEGEN_REQUIRE_VERIFY=false).
                if test_cmd:
                    ok, tout = await self._run_tests(repo_dir, test_cmd)
                    if not ok:
                        if retries_left > 0:
                            retries_left -= 1
                            message = _with_feedback(
                                initial_message, _verify_retry_message(test_cmd, tout)
                            )
                            logger.info(
                                "Verification failed for %s; retrying the edit "
                                "with the failure output.",
                                request.repo,
                            )
                            continue
                        return fail(
                            f"verification failed (`{test_cmd}`):\n"
                            f"{_tail(tout, _VERIFY_ERR_TAIL)}"
                        )
                elif self._require_verify:
                    return fail(
                        "no verification command could be established for this repo; "
                        "refusing to open an unverified PR "
                        "(set CODEGEN_REQUIRE_VERIFY=false to override)"
                    )
                else:
                    logger.warning(
                        "No verify command for %s; opening PR unverified "
                        "(CODEGEN_REQUIRE_VERIFY=false).",
                        request.repo,
                    )

                # Compute the diff for the review + pre-push gates. No diff → no PR.
                rc, names = await self._git(
                    repo_dir, ["diff", "--name-only", f"{base}..HEAD"]
                )
                changed_paths = [p for p in names.splitlines() if p.strip()]
                if rc != 0 or not changed_paths:
                    # Surface aider's own output so a no-op edit (e.g. an unreachable
                    # or misnamed model) is diagnosable from the changeset error.
                    return fail(
                        f"The agent produced no changes. aider: {_tail(out, _VERIFY_ERR_TAIL)}"
                    )
                _, diff_text = await self._git(repo_dir, ["diff", f"{base}..HEAD"])

                # Review the diff against the ORIGINAL spec before pushing: green
                # builds happily ship a token diff that implements none of the
                # task. Fail-open on infrastructure, fail-closed on judgment
                # (see app.editor.review). A deterministic revert's diff is
                # mechanically derived, so it is not judged.
                if need_review and complete is not None:
                    review_round += 1
                    prompts.append({
                        "stage": "review",
                        "label": f"Diff review (round {review_round})",
                        "system": REVIEW_SYSTEM,
                        "user": build_review_user(
                            spec=request.spec,
                            diff_text=diff_text,
                            changed_paths=changed_paths,
                        ),
                        "notes": None,
                    })
                    verdict = await review_change(
                        spec=request.spec,
                        diff_text=diff_text,
                        changed_paths=changed_paths,
                        complete=complete,
                    )
                    if not verdict.approved:
                        if retries_left > 0:
                            retries_left -= 1
                            message = _with_feedback(
                                initial_message, _review_retry_message(verdict)
                            )
                            logger.info(
                                "Quality review rejected the change for %s; "
                                "retrying the edit with the reviewer's instructions.",
                                request.repo,
                            )
                            continue
                        problems = "; ".join(verdict.problems) or verdict.fix_instructions
                        return fail(f"quality review rejected the change: {problems}")
                break

            _, numstat = await self._git(repo_dir, ["diff", "--numstat", f"{base}..HEAD"])
            diff_stat = _parse_numstat(numstat)

            # Deterministic pre-push gates on the FULL diff, before anything
            # reaches the remote: a secret-bearing or protected-path change must
            # never land on GitHub, not merely be denied a PR. (The job runner
            # re-checks the same gates as a backstop before opening the PR.)
            gate = evaluate_pre_push(
                diff_stat=diff_stat,
                changed_paths=changed_paths,
                diff_text=diff_text,
                policy=request.gates_policy,
            )
            if not gate.passed:
                return fail(
                    "pre-push gate failed; branch NOT pushed: "
                    + "; ".join(gate.violations)
                )

            # 6. Push the branch from the orchestrator (token via one-shot header).
            rc, out = await self._git(
                repo_dir,
                ["push", "origin", f"{request.branch}:{request.branch}"],
                auth_header=header,
            )
            if rc != 0:
                return fail(f"push failed: {_tail(out)}")

            return EditResult(
                success=True,
                branch=request.branch,
                diff_stat=diff_stat,
                changed_paths=changed_paths,
                diff_text=diff_text[:_DIFF_TEXT_CAP],
                prompts=prompts,
            )
        finally:
            if not keep:
                shutil.rmtree(work, ignore_errors=True)

    async def _git(
        self, cwd: Path | None, args: list[str], *, auth_header: str | None = None
    ) -> tuple[int, str]:
        env = _git_env()
        if auth_header is not None:
            # Inject the one-shot ``http.extraHeader`` via GIT_CONFIG_* env vars
            # instead of ``-c`` on the command line, so the (reversible base64)
            # token never lands on git's argv where ps / /proc would expose it.
            env = {
                **env,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": auth_header,
            }
        argv = ["git"]
        if cwd is not None:
            argv += ["-C", str(cwd)]
        argv += args
        return await self._exec(argv, cwd=None, env=env, timeout=self._git_timeout)

    async def _revert_commit(
        self, repo_dir: Path, sha: str, auth_header: str
    ) -> str | None:
        """Apply ``git revert`` of ``sha`` in the clone; return an error or None.

        The clone is shallow (depth 1 of the base branch), so the revert target
        and its parent are fetched first — GitHub serves reachable SHAs directly.
        A conflicting revert is aborted and surfaced rather than handed to the
        agent on top of a conflicted tree.
        """
        rc, out = await self._git(
            repo_dir,
            ["fetch", "--depth", "2", "origin", sha],
            auth_header=auth_header,
        )
        if rc != 0:
            return f"could not fetch revert target {sha}: {_tail(out)}"
        rc, out = await self._git(repo_dir, ["rev-list", "--parents", "-n", "1", sha])
        if rc != 0:
            return f"could not inspect revert target {sha}: {_tail(out)}"
        revert = ["revert", "--no-edit"]
        if len(out.split()) > 2:  # merge commit → revert against mainline parent 1
            revert += ["-m", "1"]
        rc, out = await self._git(repo_dir, [*revert, sha])
        if rc != 0:
            await self._git(repo_dir, ["revert", "--abort"])
            return (
                f"git revert of {sha} conflicts with later changes on the base "
                f"branch; revert it manually. {_tail(out)}"
            )
        return None

    async def _run_tests(self, repo_dir: Path, test_cmd: str) -> tuple[bool, str]:
        proc = await asyncio.create_subprocess_shell(
            test_cmd,
            cwd=str(repo_dir),
            env=_test_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self._test_timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, f"tests timed out after {self._test_timeout}s"
        return proc.returncode == 0, (stdout or b"").decode("utf-8", "replace")

    async def _exec(
        self, argv: list[str], *, cwd: Path | None, env: dict[str, str], timeout: int
    ) -> tuple[int, str]:
        """Run a subprocess; return (returncode, combined output).

        A non-zero exit is data, not an error — only spawn faults or a timeout
        surface as exceptions (caught by :meth:`implement`) or a synthetic 124.
        """
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, f"timed out after {timeout}s"
        return proc.returncode or 0, (stdout or b"").decode("utf-8", "replace")
