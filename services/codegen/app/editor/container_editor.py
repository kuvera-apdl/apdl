"""Sandboxed editor (decision D4 / Option B) — run the edit in a throwaway container.

Where :class:`~app.editor.aider_editor.AiderEditor` runs aider + the repo's tests
as subprocesses *inside the codegen API process*, ``ContainerAiderEditor``
launches an ephemeral container from the hardened sandbox image
(``Dockerfile.worker``) and runs the whole clone → aider → test → push there —
one container per changeset. The untrusted repo code therefore never executes in
the API container that holds the GitHub App key, the Postgres DSN, and the
internal token. The sandbox receives only the short-lived installation token
(kept out of the agent's reach by the reused ``AiderEditor`` token custody) and
the model provider key.

Selected when ``CODEGEN_SANDBOX=docker`` (see ``app.main``); otherwise the
in-process ``AiderEditor`` is used. It shells out to ``docker run``, so the
codegen process needs a Docker client + socket (run codegen on a Docker host, or
mount the socket for Docker-out-of-Docker).

INTEGRATION-UNTESTED, like the editor it wraps: needs a built sandbox image, a
Docker socket, a model key, and a live repo. The pure pieces (argv/env assembly,
result parsing, the never-raise contract) are unit-tested.

Hardening applied here via ``docker run`` flags: ``--rm``, ``--cap-drop ALL``,
``--security-opt no-new-privileges``, and pids/memory/cpu caps; the image runs
non-root. Egress allowlisting (GitHub + registries only; block the internal CIDR
and the cloud metadata IP) needs a network policy beyond ``docker run`` and
remains a deploy-time TODO — set ``CODEGEN_SANDBOX_NETWORK`` to a locked-down
docker network when you have one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from app.config import codegen_job_budget
from app.editor.base import EditRequest, EditResult

logger = logging.getLogger(__name__)

_DEFAULT_IMAGE = "apdl-codegen-sandbox:latest"
_ERR_TAIL = 800

# Provider keys forwarded into the sandbox by NAME only (docker reads them from
# our process env), so their VALUES never appear on the docker argv / process
# list. The GitHub App private key, Postgres DSN, and internal token are
# deliberately absent — the sandbox must not receive them.
_SECRET_ENV_FORWARD: tuple[str, ...] = (
    "OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
    "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY",
    "COHERE_API_KEY", "TOGETHERAI_API_KEY", "FIREWORKS_API_KEY", "XAI_API_KEY",
    "OLLAMA_API_BASE", "AZURE_API_KEY", "AZURE_API_BASE", "AZURE_API_VERSION",
)

# Editor knobs (non-secret) forwarded into the sandbox so the AiderEditor
# inside behaves EXACTLY like the in-process one — an operator's timeouts,
# fail-closed posture, and auxiliary-pass toggles must not silently revert to
# defaults just because CODEGEN_SANDBOX=docker. Unset values fall back to the
# same defaults in both places.
_CONFIG_ENV_FORWARD: tuple[str, ...] = (
    "CODEGEN_BRIEF",
    "CODEGEN_REVIEW",
    "CODEGEN_HELPER_MODEL",
    "CODEGEN_EDIT_RETRIES",
    "CODEGEN_REQUIRE_VERIFY",
    "CODEGEN_CACHE_PROMPTS",
    "CODEGEN_CONVENTIONS",
    "CODEGEN_TIMEOUT",
    "CODEGEN_GIT_TIMEOUT",
    "CODEGEN_LLM_TIMEOUT",
)


def _last_json(text: str) -> dict | None:
    """Return the last line of ``text`` that parses as a JSON object, else None."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


class ContainerAiderEditor:
    """Editor that runs each changeset inside an ephemeral sandbox container."""

    def __init__(self, *, image: str | None = None, docker_bin: str | None = None) -> None:
        self._image = image or os.getenv("CODEGEN_SANDBOX_IMAGE", _DEFAULT_IMAGE)
        self._docker = docker_bin or os.getenv("CODEGEN_DOCKER_BIN", "docker")
        self._model = os.getenv("CODEGEN_MODEL", "claude-opus-4-8")
        self._memory = os.getenv("CODEGEN_SANDBOX_MEMORY", "2g")
        self._cpus = os.getenv("CODEGEN_SANDBOX_CPUS", "2")
        self._pids = os.getenv("CODEGEN_SANDBOX_PIDS", "512")
        self._network = os.getenv("CODEGEN_SANDBOX_NETWORK", "")  # "" → docker default
        # The container runs the WHOLE pipeline (clone + retry rounds of
        # aider + verify + push), so its wall-clock cap is the derived job
        # budget — capping at the bare agent timeout kills legitimate retries.
        self._timeout = codegen_job_budget()

    async def implement(self, request: EditRequest) -> EditResult:
        try:
            rc, out, err = await self._run_docker(
                self._docker_argv(request), self._docker_env(request)
            )
            return self._parse_result(rc, out, err, request)
        except Exception as exc:  # an attempt must never raise to the job runner
            logger.exception("Sandboxed edit failed for %s", request.repo)
            return EditResult(success=False, branch=request.branch, error=str(exc))

    def _present_secret_keys(self) -> list[str]:
        """Provider keys actually set in our env (only these get forwarded)."""
        return [k for k in _SECRET_ENV_FORWARD if os.environ.get(k)]

    def _docker_argv(self, request: EditRequest) -> list[str]:
        """Assemble the ``docker run`` command. Secrets are passed by name only."""
        argv = [
            self._docker, "run", "--rm",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(self._pids),
            "--memory", str(self._memory),
            "--cpus", str(self._cpus),
        ]
        if self._network:
            argv += ["--network", self._network]
        # Non-secret task inputs — safe to pass as values.
        argv += [
            "-e", f"CS_REPO={request.repo}",
            "-e", f"CS_BASE={request.base_branch}",
            "-e", f"CS_BRANCH={request.branch}",
            "-e", f"CS_TITLE={request.title}",
            "-e", f"CS_SPEC={request.spec}",
            "-e", f"CS_RISK_LEVEL={request.risk_level}",
            "-e", f"CS_CONSTRAINTS={json.dumps(request.constraints)}",
            "-e", f"CODEGEN_MODEL={self._model}",
        ]
        if request.test_cmd:
            argv += ["-e", f"CS_TEST_CMD={request.test_cmd}"]
        if request.gates_policy is not None:
            argv += ["-e", f"CS_GATES_POLICY={json.dumps(request.gates_policy)}"]
        if request.revert_sha:
            argv += ["-e", f"CS_REVERT_SHA={request.revert_sha}"]
        if request.existing_branch:
            argv += ["-e", "CS_EXISTING_BRANCH=true"]
        for key in _CONFIG_ENV_FORWARD:
            if os.environ.get(key):
                argv += ["-e", f"{key}={os.environ[key]}"]
        # Secrets by NAME only → docker reads the value from our env, never argv.
        argv += ["-e", "GH_TOKEN"]
        for key in self._present_secret_keys():
            argv += ["-e", key]
        argv.append(self._image)
        return argv

    def _docker_env(self, request: EditRequest) -> dict[str, str]:
        """The environment for the ``docker`` client process (carries the secrets)."""
        env = {"PATH": os.environ.get("PATH", os.defpath)}
        for key in ("HOME", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            if key in os.environ:
                env[key] = os.environ[key]
        env["GH_TOKEN"] = request.token
        for key in self._present_secret_keys():
            env[key] = os.environ[key]
        return env

    def _parse_result(
        self, rc: int, stdout: str, stderr: str, request: EditRequest
    ) -> EditResult:
        data = _last_json(stdout)
        if data is not None:
            return EditResult(
                success=bool(data.get("success")),
                branch=data.get("branch") or request.branch,
                diff_stat=data.get("diff_stat") or {},
                changed_paths=data.get("changed_paths") or [],
                diff_text=data.get("diff_text") or "",
                error=data.get("error"),
                logs_uri=data.get("logs_uri"),
                head_sha=data.get("head_sha"),
            )
        tail = (stderr or stdout or "").strip()[-_ERR_TAIL:]
        return EditResult(
            success=False,
            branch=request.branch,
            error=f"sandbox produced no result (exit {rc}): {tail}",
        )

    async def _run_docker(
        self, argv: list[str], env: dict[str, str]
    ) -> tuple[int, str, str]:
        """Run ``docker run`` keeping stdout (the JSON result) and stderr separate."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", f"sandbox timed out after {self._timeout}s"
        return (
            proc.returncode or 0,
            (out or b"").decode("utf-8", "replace"),
            (err or b"").decode("utf-8", "replace"),
        )
