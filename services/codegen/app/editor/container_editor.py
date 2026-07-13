"""Sandboxed editor (decision D4 / Option B) — run the edit in a throwaway container.

Where :class:`~app.editor.aider_editor.AiderEditor` runs Aider inside the
codegen API process, ``ContainerAiderEditor``
launches an ephemeral container from the hardened sandbox image
(``Dockerfile.worker``) and runs the whole clone → Aider → gate → push there —
one container per changeset. The untrusted repo code therefore never executes in
the API container that holds the GitHub App key, the Postgres DSN, and the
internal token. The sandbox receives only the short-lived installation token
(kept out of the agent's reach by the reused ``AiderEditor`` token custody) and
the model provider key.

Selected by default with ``CODEGEN_SANDBOX=docker`` (see ``app.main``). The
trusted local in-process mode requires an explicit opt-in. It shells out to
``docker run``, so the codegen process needs a Docker client + socket (run
codegen on a Docker host, or mount the socket for Docker-out-of-Docker).

INTEGRATION-UNTESTED, like the editor it wraps: needs a built sandbox image, a
Docker socket, a model key, and a live repo. The pure pieces (argv/env assembly,
result parsing, the never-raise contract) are unit-tested.

Hardening applied here via ``docker run`` flags: ``--rm``, a read-only root,
writable no-exec tmpfs mounts, ``--cap-drop ALL``, ``--security-opt
no-new-privileges``, and pids/memory/cpu caps; the image runs non-root. PR
publication stages additionally require a named network instead of a Docker
default network. Evaluated stages require the operator to enforce the egress
policy; local development uses a separate development-only bridge.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import uuid

from app.config import codegen_job_budget
from app.contracts.models import ContractBundle
from app.editor.base import EditRequest, EditResult
from app.editor.environment import CODEGEN_BEHAVIOR_ENV, MODEL_PROVIDER_ENV
from app.inspection.models import DependencySlice, InspectionSnapshot
from app.requirements.models import RequirementLedger
from app.runtime.models import (
    GeneratedRuntimeWorkflowAttestation,
    RuntimeAcceptancePlan,
)
from app.semantic_review.models import ReviewVerdict
from app.verification.models import VerificationCoverage, VerificationPlan

logger = logging.getLogger(__name__)

_DEFAULT_IMAGE = "apdl-codegen-sandbox:latest"
_ERR_TAIL = 800

# Provider keys forwarded into the sandbox by NAME only (docker reads them from
# our process env), so their VALUES never appear on the docker argv / process
# list. The GitHub App private key, Postgres DSN, and internal token are
# deliberately absent — the sandbox must not receive them.
_SECRET_ENV_FORWARD: tuple[str, ...] = MODEL_PROVIDER_ENV

# Editor knobs (non-secret) forwarded into the sandbox so the AiderEditor
# inside behaves EXACTLY like the in-process one — an operator's timeouts,
# fail-closed posture, and auxiliary-pass toggles must not silently revert to
# defaults just because CODEGEN_SANDBOX=docker. Unset values fall back to the
# same defaults in both places.
_CONFIG_ENV_FORWARD: tuple[str, ...] = tuple(
    key for key in CODEGEN_BEHAVIOR_ENV if key not in {"CODEGEN_MODEL"}
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

    def assert_runtime_ready(
        self,
        *,
        expected_revision: str,
        require_immutable_image: bool = True,
    ) -> None:
        """Fail PR-stage startup unless Docker, image, and network are real.

        Evaluated stages additionally require the exact immutable sandbox image
        bound into their evidence. Local development may use a rebuilt tag, but
        it still validates the daemon, image revision label, and isolated named
        network before the API accepts work. Offline/shadow can boot without a
        Docker daemon because their changeset endpoints are disabled.
        """
        if not expected_revision or expected_revision == "development-unversioned":
            raise RuntimeError("PR rollout requires an immutable CODEGEN_REVISION")
        if self._network in {"", "bridge", "default", "host", "none"}:
            raise RuntimeError("PR rollout requires a non-built-in sandbox network")
        if require_immutable_image and not re.search(
            r"(?:@|^)sha256:[0-9a-f]{64}$", self._image
        ):
            raise RuntimeError("PR rollout requires an immutable sandbox image digest")

        def inspect(*args: str) -> str:
            try:
                completed = subprocess.run(
                    [self._docker, *args],
                    env=self._docker_control_env(),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError("Codegen Docker runtime preflight failed") from exc
            if completed.returncode != 0:
                raise RuntimeError("Codegen Docker runtime preflight failed")
            return completed.stdout.strip()

        inspect("version", "--format", "{{.Server.Version}}")
        observed_revision = inspect(
            "image",
            "inspect",
            "--format",
            '{{ index .Config.Labels "dev.apdl.codegen.revision" }}',
            self._image,
        )
        if observed_revision != expected_revision:
            raise RuntimeError(
                "Codegen sandbox image revision does not match CODEGEN_REVISION"
            )
        inspect("network", "inspect", self._network)

    async def implement(self, request: EditRequest) -> EditResult:
        try:
            container_name = f"apdl-codegen-{uuid.uuid4().hex}"
            rc, out, err = await self._run_docker(
                self._docker_argv(request, container_name=container_name),
                self._docker_env(request),
                container_name=container_name,
            )
            return self._parse_result(rc, out, err, request)
        except Exception as exc:  # an attempt must never raise to the job runner
            logger.exception("Sandboxed edit failed for %s", request.repo)
            return EditResult(success=False, branch=request.branch, error=str(exc))

    def _present_secret_keys(self) -> list[str]:
        """Provider keys actually set in our env (only these get forwarded)."""
        return [k for k in _SECRET_ENV_FORWARD if os.environ.get(k)]

    def _docker_argv(
        self,
        request: EditRequest,
        *,
        container_name: str | None = None,
    ) -> list[str]:
        """Assemble the ``docker run`` command. Secrets are passed by name only."""
        argv = [
            self._docker, "run", "--rm",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(self._pids),
            "--memory", str(self._memory),
            "--cpus", str(self._cpus),
            "--tmpfs", "/workspace:rw,nosuid,nodev,noexec,size=4g,uid=1000,gid=1000",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=512m,uid=1000,gid=1000",
            "--user", "1000:1000",
        ]
        if container_name is not None:
            argv += ["--name", container_name]
        if self._network:
            argv += ["--network", self._network]
        # Non-secret task inputs — safe to pass as values.
        argv += [
            "-e", f"CS_REPO={request.repo}",
            "-e", f"CS_PROJECT_SCOPE={request.project_scope or request.repo}",
            "-e", f"CS_BASE={request.base_branch}",
            "-e", f"CS_BRANCH={request.branch}",
            "-e", f"CS_TITLE={request.title}",
            "-e", f"CS_SPEC={request.spec}",
            "-e", f"CS_RISK_LEVEL={request.risk_level}",
            "-e", f"CS_CONSTRAINTS={json.dumps(request.constraints)}",
            "-e", f"CODEGEN_MODEL={self._model}",
            "-e", "HOME=/workspace/home",
            "-e", "TMPDIR=/workspace/tmp",
        ]
        if request.test_cmd:
            argv += ["-e", f"CS_TEST_CMD={request.test_cmd}"]
        argv += [
            "-e",
            "CS_SAFETY_POLICY="
            + json.dumps(request.safety_policy.model_dump(mode="json")),
            "-e",
            f"CS_SAFETY_POLICY_SHA256={request.safety_policy.canonical_digest()}",
        ]
        if request.revert_sha:
            argv += ["-e", f"CS_REVERT_SHA={request.revert_sha}"]
        if request.existing_branch:
            argv += ["-e", "CS_EXISTING_BRANCH=true"]
        if request.expected_head_sha:
            argv += ["-e", f"CS_EXPECTED_HEAD_SHA={request.expected_head_sha}"]
        if request.requirement_ledger is not None:
            argv += [
                "-e",
                "CS_REQUIREMENT_LEDGER="
                + json.dumps(request.requirement_ledger.model_dump(mode="json")),
            ]
        if request.runtime_acceptance_plan is not None:
            argv += [
                "-e",
                "CS_RUNTIME_ACCEPTANCE_PLAN="
                + json.dumps(request.runtime_acceptance_plan.model_dump(mode="json")),
            ]
        argv += [
            "-e",
            "CS_RUNTIME_ACCEPTANCE_POLICY="
            + json.dumps(request.runtime_acceptance_policy.model_dump(mode="json")),
        ]
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
        env = self._docker_control_env()
        env["GH_TOKEN"] = request.token
        for key in self._present_secret_keys():
            env[key] = os.environ[key]
        return env

    @staticmethod
    def _docker_control_env() -> dict[str, str]:
        """Docker client environment without repository or provider credentials."""
        env = {"PATH": os.environ.get("PATH", os.defpath)}
        for key in ("HOME", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            if key in os.environ:
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
                prompts=data.get("prompts") or [],
                contract_bundle=(
                    ContractBundle.model_validate(data["contract_bundle"])
                    if data.get("contract_bundle") is not None
                    else None
                ),
                requirement_ledger=(
                    RequirementLedger.model_validate_json(
                        json.dumps(data["requirement_ledger"])
                    )
                    if data.get("requirement_ledger") is not None
                    else None
                ),
                inspection_snapshot=(
                    InspectionSnapshot.model_validate(data["inspection_snapshot"])
                    if data.get("inspection_snapshot") is not None
                    else None
                ),
                dependency_slice=(
                    DependencySlice.model_validate(data["dependency_slice"])
                    if data.get("dependency_slice") is not None
                    else None
                ),
                verification_plan=(
                    VerificationPlan.model_validate_json(
                        json.dumps(data["verification_plan"])
                    )
                    if data.get("verification_plan") is not None
                    else None
                ),
                verification_coverage=(
                    VerificationCoverage.model_validate_json(
                        json.dumps(data["verification_coverage"])
                    )
                    if data.get("verification_coverage") is not None
                    else None
                ),
                runtime_acceptance_plan=(
                    RuntimeAcceptancePlan.model_validate_json(
                        json.dumps(data["runtime_acceptance_plan"])
                    )
                    if data.get("runtime_acceptance_plan") is not None
                    else None
                ),
                generated_runtime_workflow=(
                    GeneratedRuntimeWorkflowAttestation.model_validate_json(
                        json.dumps(data["generated_runtime_workflow"])
                    )
                    if data.get("generated_runtime_workflow") is not None
                    else None
                ),
                review_verdict=(
                    ReviewVerdict.model_validate_json(
                        json.dumps(data["review_verdict"])
                    )
                    if data.get("review_verdict") is not None
                    else None
                ),
            )
        tail = (stderr or stdout or "").strip()[-_ERR_TAIL:]
        return EditResult(
            success=False,
            branch=request.branch,
            error=f"sandbox produced no result (exit {rc}): {tail}",
        )

    async def _run_docker(
        self,
        argv: list[str],
        env: dict[str, str],
        *,
        container_name: str,
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
            await self._stop_container_and_client(container_name, proc)
            return 124, "", f"sandbox timed out after {self._timeout}s"
        except asyncio.CancelledError:
            await self._stop_container_and_client(container_name, proc)
            raise
        return (
            proc.returncode or 0,
            (out or b"").decode("utf-8", "replace"),
            (err or b"").decode("utf-8", "replace"),
        )

    async def _stop_container_and_client(
        self,
        container_name: str,
        client_process: asyncio.subprocess.Process,
    ) -> None:
        """Reap the creating CLI, then force-remove its named container."""
        # Stop the creator first. Otherwise an early cancellation can race:
        # `docker rm` observes no container, then the still-running `docker run`
        # client creates one after cleanup has already returned.
        if client_process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                client_process.terminate()
            try:
                await asyncio.wait_for(client_process.wait(), timeout=5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    client_process.kill()
                await client_process.wait()

        # With the creator reaped, this final remove cannot race a later start.
        # Retry once and distinguish an already-absent container from a daemon
        # failure. Never claim cleanup succeeded while a provider-key-bearing
        # sandbox is still running.
        last_detail = ""
        for attempt in range(2):
            remove_rc, remove_detail = await self._docker_control_command(
                "rm", "-f", container_name
            )
            if remove_rc == 0:
                return
            inspect_rc, inspect_detail = await self._docker_control_command(
                "inspect", "--type", "container", container_name
            )
            combined = f"{remove_detail}\n{inspect_detail}".strip()
            if inspect_rc not in (None, 0) and self._container_is_absent(combined):
                return
            last_detail = combined[-400:]
            logger.warning(
                "Codegen sandbox %s removal attempt %d was not verified: %s",
                container_name,
                attempt + 1,
                last_detail,
            )

        logger.critical(
            "Credential-bearing Codegen sandbox %s may still be running: %s",
            container_name,
            last_detail,
        )
        raise RuntimeError(
            f"Could not verify removal of Codegen sandbox {container_name}"
        )

    async def _docker_control_command(self, *args: str) -> tuple[int | None, str]:
        """Run a credential-free Docker control command with bounded output."""
        try:
            process = await asyncio.create_subprocess_exec(
                self._docker,
                *args,
                env=self._docker_control_env(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _out, error = await asyncio.wait_for(
                    process.communicate(), timeout=30
                )
            except asyncio.TimeoutError:
                process.kill()
                _out, error = await process.communicate()
                return None, "Docker control command timed out"
            return (
                process.returncode,
                (error or b"").decode("utf-8", "replace")[-400:],
            )
        except Exception as exc:
            logger.exception("Docker control command failed: %s", " ".join(args))
            return None, str(exc)[-400:]

    @staticmethod
    def _container_is_absent(detail: str) -> bool:
        normalized = detail.casefold()
        return "no such container" in normalized or "no such object" in normalized
