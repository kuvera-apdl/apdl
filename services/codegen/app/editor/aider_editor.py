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
import logging
import os
import shutil
import tempfile
from pathlib import Path

from app import config
from app.editor.base import EditRequest, EditResult

logger = logging.getLogger(__name__)

_DIFF_TEXT_CAP = 1_000_000  # cap the diff text fed to the secret scan (chars)
_ERR_TAIL = 800  # how much subprocess output to surface on failure

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
_TEST_DETECTORS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "python -m pytest -q"),
    ("pytest.ini", "python -m pytest -q"),
    ("tox.ini", "python -m pytest -q"),
    ("package.json", "npm test --silent"),
    ("go.mod", "go test ./..."),
    ("Cargo.toml", "cargo test"),
)


def _basic_auth_header(token: str) -> str:
    """GitHub App token → a one-shot Basic auth header value (never persisted)."""
    raw = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"AUTHORIZATION: basic {raw}"


def _agent_env() -> dict[str, str]:
    """Minimal environment for the agent/test subprocess — LLM keys only."""
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env.setdefault("PATH", os.defpath)
    env["AIDER_ANALYTICS"] = "false"  # headless: no phone-home / update prompts
    env["AIDER_CHECK_UPDATE"] = "false"
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


def _detect_test_cmd(repo_dir: Path) -> str | None:
    """Best-effort repo test command when the connection policy sets none."""
    makefile = repo_dir / "Makefile"
    if makefile.is_file():
        try:
            text = makefile.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        if any(line.startswith("test:") for line in text.splitlines()):
            return "make test"
    for filename, cmd in _TEST_DETECTORS:
        if (repo_dir / filename).is_file():
            return cmd
    return None


def _build_message(spec: str, constraints: list[str]) -> str:
    """Compose the single headless instruction handed to Aider."""
    message = spec.strip()
    if constraints:
        bullets = "\n".join(f"- {c}" for c in constraints)
        message = f"{message}\n\nConstraints:\n{bullets}"
    return message


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
    ) -> None:
        self._model = model or config.codegen_model()
        self._aider_bin = aider_bin or config.codegen_aider_bin()
        self._workdir_base = workdir_base or config.codegen_workdir()
        self._git_timeout = config.codegen_git_timeout()
        self._agent_timeout = config.codegen_agent_timeout()
        self._test_timeout = config.codegen_test_timeout()

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

        def fail(error: str) -> EditResult:
            return EditResult(success=False, branch=request.branch, error=error)

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
                return fail(f"clone failed: {out.strip()[-_ERR_TAIL:]}")
            rc, out = await self._git(repo_dir, ["checkout", "-b", request.branch])
            if rc != 0:
                return fail(f"branch failed: {out.strip()[-_ERR_TAIL:]}")
            # Local commit identity so Aider's commits succeed without a global config.
            await self._git(repo_dir, ["config", "user.email", "codegen@apdl.dev"])
            await self._git(repo_dir, ["config", "user.name", "APDL Codegen"])

            # 2. Resolve the test command: connection policy first, then detect.
            test_cmd = request.test_cmd or _detect_test_cmd(repo_dir)

            # 3. Run Aider headless. It edits + commits locally; it does NOT push.
            #    The settings file lives outside repo_dir so it never enters the diff.
            settings_file = work / "aider.model.settings.yml"
            settings_file.write_text(_model_settings_yaml(self._model), encoding="utf-8")
            argv = [self._aider_bin, "--model", self._model,
                    "--model-settings-file", str(settings_file),
                    "--yes-always", "--no-stream", "--no-pretty"]
            if test_cmd:
                argv += ["--auto-test", "--test-cmd", test_cmd]
            argv += ["--message", _build_message(request.spec, request.constraints)]
            rc, out = await self._exec(
                argv, cwd=repo_dir, env=_agent_env(), timeout=self._agent_timeout
            )
            if rc != 0:
                return fail(f"aider exited {rc}: {out.strip()[-_ERR_TAIL:]}")

            # 4. Verify tests are actually green, outside the agent's control.
            if test_cmd:
                ok, tout = await self._run_tests(repo_dir, test_cmd)
                if not ok:
                    return fail(f"tests failed: {tout.strip()[-_ERR_TAIL:]}")
            else:
                logger.warning(
                    "No test command for %s; opening PR without a verify step.",
                    request.repo,
                )

            # 5. Compute the diff for the pre-push gates. No diff → no PR.
            base = request.base_branch
            rc, names = await self._git(repo_dir, ["diff", "--name-only", f"{base}..HEAD"])
            changed_paths = [p for p in names.splitlines() if p.strip()]
            if rc != 0 or not changed_paths:
                # Surface aider's own output so a no-op edit (e.g. an unreachable
                # or misnamed model) is diagnosable from the changeset error.
                return fail(f"The agent produced no changes. aider: {out.strip()[-_ERR_TAIL:]}")
            _, numstat = await self._git(repo_dir, ["diff", "--numstat", f"{base}..HEAD"])
            _, diff_text = await self._git(repo_dir, ["diff", f"{base}..HEAD"])

            # 6. Push the branch from the orchestrator (token via one-shot header).
            rc, out = await self._git(
                repo_dir,
                ["push", "origin", f"{request.branch}:{request.branch}"],
                auth_header=header,
            )
            if rc != 0:
                return fail(f"push failed: {out.strip()[-_ERR_TAIL:]}")

            return EditResult(
                success=True,
                branch=request.branch,
                diff_stat=_parse_numstat(numstat),
                changed_paths=changed_paths,
                diff_text=diff_text[:_DIFF_TEXT_CAP],
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

    async def _run_tests(self, repo_dir: Path, test_cmd: str) -> tuple[bool, str]:
        proc = await asyncio.create_subprocess_shell(
            test_cmd,
            cwd=str(repo_dir),
            env=_agent_env(),
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
