"""Sandboxed editor (decision D4 / Option B) — run the edit in a throwaway container.

Where :class:`~app.editor.aider_editor.AiderEditor` runs Aider inside the
codegen API process, ``ContainerAiderEditor``
launches an ephemeral container from the hardened sandbox image
(``Dockerfile.worker``) and runs clone → Aider → gate there —
after a separate provider-free inspection container attests the exact source
tree. The untrusted repo code therefore never executes in
the API container that holds the GitHub App key, the Postgres DSN, and the
internal token. The inspection credential and the editor's complete task
request are consumed from bounded stdin contracts rather than process metadata.
Only the second container receives the model provider key. It returns a binary
patch and Git object identities;
the controller reconstructs and publishes the approved tree with a separate
just-in-time write credential.

Selected by default with ``CODEGEN_SANDBOX=docker`` (see ``app.main``). The
trusted local in-process mode requires an explicit opt-in. It shells out to
``docker run``, so the codegen process needs a Docker client + socket (run
codegen on a Docker host, or mount the socket for Docker-out-of-Docker).

The worker image's real Docker launch, writable-workspace, hardening, and
verified-cleanup path has a daemon-backed smoke contract. A live model/repository
edit still requires deployment credentials and remains an external integration.

Hardening applied here via ``docker run`` flags: ``--rm``, ``--network none``
for evaluated work, a read-only root,
writable no-exec tmpfs mounts, ``--cap-drop ALL``, ``--security-opt
no-new-privileges``, and pids/memory/cpu caps; the image runs non-root.
Evaluated stages mount only an attested proxy Unix-socket volume read-only and
start a sealed loopback relay. Local development uses a separate
development-only bridge.
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
from dataclasses import replace

from app.config import (
    codegen_controller_image_id,
    codegen_egress_policy_sha256,
    codegen_egress_proxy_image_id,
    codegen_egress_proxy_url,
    codegen_egress_socket_volume,
    codegen_job_budget,
)
from app.contracts.models import ContractBundle
from app.egress import (
    EgressPolicyAttestation,
    attest_docker_egress_policy,
    proxy_environment,
    relay_command,
    worker_socket_mount,
)
from app.editor.base import EditRequest, EditResult
from app.editor.environment import (
    CODEGEN_BEHAVIOR_ENV,
    resolve_model_provider_environment,
)
from app.editor.excerpts import DEFAULT_ERROR_TAIL_CHARS, tail_excerpt
from app.editor.worker_contract import (
    encode_codegen_worker_request,
    validate_codegen_worker_request_source,
)
from app.inspection.models import DependencySlice, InspectionSnapshot
from app.inspection.preflight import RepositoryPreflightAttestation
from app.requirements.models import RequirementLedger
from app.runtime.models import (
    GeneratedRuntimeWorkflowAttestation,
    RuntimeAcceptancePlan,
)
from app.semantic_review.models import ReviewVerdict
from app.verification.models import VerificationCoverage, VerificationPlan

logger = logging.getLogger(__name__)

_DEFAULT_IMAGE = "apdl-codegen-sandbox:latest"
_ERR_TAIL = DEFAULT_ERROR_TAIL_CHARS

# Provider keys forwarded into the sandbox by NAME only (docker reads them from
# our process env), so their VALUES never appear on the docker argv / process
# list. The GitHub App private key, Postgres DSN, and internal token are
# deliberately absent — the sandbox must not receive them.
# Editor knobs (non-secret) forwarded into the sandbox so the AiderEditor
# inside behaves EXACTLY like the in-process one — an operator's timeouts,
# fail-closed posture, and auxiliary-pass toggles must not silently revert to
# defaults just because CODEGEN_SANDBOX=docker. Unset values fall back to the
# same defaults in both places.
_CONFIG_ENV_FORWARD: tuple[str, ...] = tuple(
    key
    for key in CODEGEN_BEHAVIOR_ENV
    if key not in {"CODEGEN_MODEL", "CODEGEN_HELPER_MODEL"}
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

    def __init__(
        self, *, image: str | None = None, docker_bin: str | None = None
    ) -> None:
        self._image = image or os.getenv("CODEGEN_SANDBOX_IMAGE", _DEFAULT_IMAGE)
        self._docker = docker_bin or os.getenv("CODEGEN_DOCKER_BIN", "docker")
        self._model = os.getenv("CODEGEN_MODEL", "claude-opus-4-8")
        self._helper_model = os.getenv("CODEGEN_HELPER_MODEL") or self._model
        self._memory = os.getenv("CODEGEN_SANDBOX_MEMORY", "2g")
        self._cpus = os.getenv("CODEGEN_SANDBOX_CPUS", "2")
        self._pids = os.getenv("CODEGEN_SANDBOX_PIDS", "512")
        self._network = os.getenv("CODEGEN_SANDBOX_NETWORK", "")  # "" → docker default
        self._egress_policy_sha256 = codegen_egress_policy_sha256()
        self._egress_proxy_image_id = codegen_egress_proxy_image_id()
        self._egress_socket_volume = codegen_egress_socket_volume()
        self._controller_image_id = codegen_controller_image_id()
        self._egress_proxy_url = codegen_egress_proxy_url()
        configured_egress = bool(
            self._egress_policy_sha256
            or self._egress_proxy_image_id
            or self._egress_socket_volume
            or self._controller_image_id
        )
        if configured_egress and not (
            self._egress_policy_sha256
            and self._egress_proxy_image_id
            and self._egress_socket_volume
            and self._controller_image_id
        ):
            raise ValueError(
                "Codegen egress policy, proxy image, controller image, and socket "
                "volume must be configured together"
            )
        if configured_egress and self._network:
            raise ValueError(
                "evaluated workers use Docker --network none; "
                "CODEGEN_SANDBOX_NETWORK must be empty"
            )
        self._proxy_environment = (
            proxy_environment(self._egress_proxy_url) if configured_egress else {}
        )
        self._egress_attestation: EgressPolicyAttestation | None = None
        # The container runs the worker pipeline (clone + retry rounds of
        # aider + verify + patch export), so its wall-clock cap is the derived job
        # budget — capping at the bare agent timeout kills legitimate retries.
        self._timeout = codegen_job_budget()

    def assert_runtime_ready(
        self,
        *,
        expected_revision: str,
        require_immutable_image: bool = True,
        require_egress_policy: bool = True,
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
        if require_egress_policy:
            if not (
                self._egress_policy_sha256
                and self._egress_proxy_image_id
                and self._egress_socket_volume
                and self._controller_image_id
            ):
                raise RuntimeError(
                    "evaluated PR rollout requires an attested Codegen egress policy"
                )
            self._egress_attestation = self._attest_egress_policy(
                launch_id="codegen-runtime-startup"
            )
        else:
            if self._network in {"", "bridge", "default", "host", "none"}:
                raise RuntimeError(
                    "development PR rollout requires a non-built-in sandbox network"
                )
            inspect("network", "inspect", self._network)

    @property
    def egress_attestation(self) -> EgressPolicyAttestation | None:
        return self._egress_attestation

    def _attest_egress_policy(self, *, launch_id: str) -> EgressPolicyAttestation:
        if not (
            self._egress_policy_sha256
            and self._egress_proxy_image_id
            and self._egress_socket_volume
            and self._controller_image_id
        ):
            raise RuntimeError(
                "evaluated worker launch requires an attested Codegen egress policy"
            )
        return attest_docker_egress_policy(
            docker_bin=self._docker,
            probe_image=self._controller_image_id,
            launch_id=launch_id,
            socket_volume=self._egress_socket_volume,
            expected_policy_sha256=self._egress_policy_sha256,
            expected_proxy_image_id=self._egress_proxy_image_id,
            proxy_url=self._egress_proxy_url,
            environment=self._docker_control_env(),
        )

    async def implement(self, request: EditRequest) -> EditResult:
        try:
            # Validate and bound all task-bearing input before even the
            # provider-free inspection container is allowed to start.
            validate_codegen_worker_request_source(request)
            run_id = uuid.uuid4().hex
            inspection_name = f"apdl-codegen-inspect-{run_id}"
            if self._proxy_environment:
                # Re-check immediately before both workers. A relay attached or
                # topology mutated after service startup must fail this job closed.
                self._egress_attestation = await asyncio.to_thread(
                    self._attest_egress_policy,
                    launch_id=inspection_name,
                )
            rc, out, err = await self._run_docker(
                self._preflight_argv(request, container_name=inspection_name),
                self._docker_control_env(),
                container_name=inspection_name,
                stdin_data=self._preflight_credential_envelope(request.token),
            )
            attestation = self._parse_preflight_result(rc, out, err, request)
            editor_request = replace(
                request,
                repository_preflight=attestation,
            )
            container_name = f"apdl-codegen-edit-{run_id}"
            if self._proxy_environment:
                # The inspection container may run long enough for the network
                # to change. Re-attest after it exits and immediately before
                # the model-bearing editor is launched.
                self._egress_attestation = await asyncio.to_thread(
                    self._attest_egress_policy,
                    launch_id=container_name,
                )
            worker_input = encode_codegen_worker_request(editor_request)
            rc, out, err = await self._run_docker(
                self._docker_argv(editor_request, container_name=container_name),
                self._docker_env(editor_request),
                container_name=container_name,
                stdin_data=worker_input,
            )
            return self._parse_result(rc, out, err, editor_request)
        except Exception as exc:  # an attempt must never raise to the job runner
            logger.exception("Sandboxed edit failed for %s", request.repo)
            return EditResult(success=False, branch=request.branch, error=str(exc))

    def _selected_provider_environment(self) -> dict[str, str]:
        """Resolve the exact credential/routing union required by both models."""
        return resolve_model_provider_environment(
            os.environ,
            model=self._model,
            helper_model=self._helper_model,
        )

    def _docker_argv(
        self,
        request: EditRequest,
        *,
        container_name: str | None = None,
    ) -> list[str]:
        """Assemble the model-bearing editor command after source attestation."""
        if request.repository_preflight is None:
            raise ValueError("sandbox editor requires repository preflight evidence")
        argv = self._sandbox_argv(container_name=container_name, role="editor")
        # Task data is carried exclusively by the bounded stdin contract. This
        # argv contains only sandbox/runtime configuration and provider-key
        # names, never task text, repository authority, or tenant policy.
        argv += [
            "-e",
            f"CODEGEN_MODEL={self._model}",
            "-e",
            f"CODEGEN_HELPER_MODEL={self._helper_model}",
            "-e",
            "HOME=/workspace/home",
            "-e",
            "TMPDIR=/workspace/tmp",
        ]
        for name, value in self._proxy_environment.items():
            argv += ["-e", f"{name}={value}"]
        for key in _CONFIG_ENV_FORWARD:
            if os.environ.get(key):
                argv += ["-e", f"{key}={os.environ[key]}"]
        # Provider secrets are forwarded by NAME only. Repository authority is
        # absent from argv and environ and is consumed from stdin instead.
        for key in self._selected_provider_environment():
            argv += ["-e", key]
        if self._proxy_environment:
            argv += [
                "--entrypoint",
                "python",
                self._image,
                *relay_command(["python", "/app/run_changeset.py"]),
            ]
        else:
            argv.append(self._image)
        return argv

    def _sandbox_argv(
        self,
        *,
        container_name: str | None,
        role: str,
    ) -> list[str]:
        """Common hardening for isolated inspection and editor containers."""
        argv = [
            self._docker,
            "run",
            "--rm",
            "-i",
            # Docker's default is an isolated PID namespace. There is no
            # portable `--pid private` value (Docker 29 rejects it), so leave
            # the option unset rather than turning every worker launch into an
            # invalid command.
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self._pids),
            "--memory",
            str(self._memory),
            "--cpus",
            str(self._cpus),
            "--tmpfs",
            "/workspace:rw,nosuid,nodev,noexec,size=4g,uid=1000,gid=1000",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=512m,uid=1000,gid=1000",
            "--user",
            "1000:1000",
            "--label",
            f"dev.apdl.codegen.role={role}",
        ]
        if container_name is not None:
            argv += ["--name", container_name]
        if self._proxy_environment:
            argv += [
                "--network",
                "none",
                "--mount",
                worker_socket_mount(self._egress_socket_volume),
            ]
        elif self._network:
            argv += ["--network", self._network]
        return argv

    def _preflight_argv(
        self,
        request: EditRequest,
        *,
        container_name: str | None = None,
    ) -> list[str]:
        """Build the provider-free repository-inspection container command."""
        source_branch = (
            request.branch if request.existing_branch else request.base_branch
        )
        argv = self._sandbox_argv(container_name=container_name, role="inspection")
        argv += [
            "-e",
            f"CS_REPO={request.repo}",
            "-e",
            f"CS_SOURCE_BRANCH={source_branch}",
            "-e",
            "HOME=/workspace/home",
            "-e",
            "TMPDIR=/workspace/tmp",
        ]
        for name, value in self._proxy_environment.items():
            argv += ["-e", f"{name}={value}"]
        argv += ["--entrypoint", "python", self._image]
        if self._proxy_environment:
            argv += relay_command(["python", "-m", "app.inspection.preflight_cli"])
        else:
            argv += ["-m", "app.inspection.preflight_cli"]
        return argv

    def _docker_env(self, request: EditRequest) -> dict[str, str]:
        """Docker client environment carrying provider keys, never Git authority."""
        env = self._docker_control_env()
        env.update(self._selected_provider_environment())
        return env

    @staticmethod
    def _preflight_credential_envelope(token: str) -> bytes:
        return (json.dumps({"read_token": token}) + "\n").encode()

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
                base_sha=data.get("base_sha"),
                candidate_tree_sha=data.get("candidate_tree_sha"),
                patch_base64=data.get("patch_base64"),
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
        tail = tail_excerpt(stderr or stdout or "", limit=_ERR_TAIL)
        return EditResult(
            success=False,
            branch=request.branch,
            error=f"sandbox produced no result (exit {rc}): {tail}",
        )

    def _parse_preflight_result(
        self,
        rc: int,
        stdout: str,
        stderr: str,
        request: EditRequest,
    ) -> RepositoryPreflightAttestation:
        """Validate the first container's metadata-only inspection result."""
        data = _last_json(stdout)
        if rc != 0 or data is None or data.get("success") is not True:
            if isinstance(data, dict) and isinstance(data.get("error"), str):
                detail = data["error"]
            else:
                detail = tail_excerpt(stderr or stdout or "", limit=_ERR_TAIL)
            raise RuntimeError(f"repository preflight failed: {detail}")
        attestation = RepositoryPreflightAttestation.model_validate(
            data.get("attestation")
        )
        source_branch = (
            request.branch if request.existing_branch else request.base_branch
        )
        if (
            attestation.repository != request.repo
            or attestation.source_branch != source_branch
        ):
            raise RuntimeError("repository preflight identity mismatch")
        return attestation

    async def _run_docker(
        self,
        argv: list[str],
        env: dict[str, str],
        *,
        container_name: str,
        stdin_data: bytes | None = None,
    ) -> tuple[int, str, str]:
        """Run ``docker run`` keeping stdout (the JSON result) and stderr separate."""
        spawn_task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdin=(
                    asyncio.subprocess.PIPE
                    if stdin_data is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )
        try:
            proc = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            # The subprocess creation coroutine may have crossed into Docker
            # before cancellation was delivered. Wait for that race to settle,
            # then reap the client and verify the deterministic name is absent.
            try:
                await self._finish_spawn_cleanup_uninterruptibly(
                    container_name,
                    spawn_task,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Sandbox spawn cleanup failed while cancellation was "
                    "pending for %s",
                    container_name,
                )
            raise
        try:
            communication = (
                proc.communicate(stdin_data)
                if stdin_data is not None
                else proc.communicate()
            )
            out, err = await asyncio.wait_for(communication, timeout=self._timeout)
        except asyncio.TimeoutError:
            await self._finish_cleanup_uninterruptibly(container_name, proc)
            return 124, "", f"sandbox timed out after {self._timeout}s"
        except asyncio.CancelledError:
            # Cancellation is preserved, but cleanup is a bounded security
            # obligation. Repeated cancellation must not interrupt docker rm -f.
            try:
                await self._finish_cleanup_uninterruptibly(container_name, proc)
            except Exception:
                logger.exception(
                    "Sandbox cleanup failed while cancellation was pending for %s",
                    container_name,
                )
            raise
        except Exception:
            await self._finish_cleanup_uninterruptibly(container_name, proc)
            raise
        result = (
            proc.returncode or 0,
            (out or b"").decode("utf-8", "replace"),
            (err or b"").decode("utf-8", "replace"),
        )
        # `docker run --rm` is not sufficient evidence of cleanup: the client
        # can lose its daemon stream and exit while the named container keeps
        # running. Reap first, then force-remove and verify absence on every
        # completed path before returning the captured result.
        await self._finish_cleanup_uninterruptibly(container_name, proc)
        return result

    async def _finish_spawn_cleanup_uninterruptibly(
        self,
        container_name: str,
        spawn_task: asyncio.Task[asyncio.subprocess.Process],
    ) -> None:
        """Settle a cancelled Docker spawn and verify its named container absent."""

        async def cleanup() -> None:
            try:
                client_process = await spawn_task
            except (asyncio.CancelledError, Exception):
                # Even an unsuccessful client spawn can be ambiguous at the
                # daemon boundary, so absence of the reserved name is checked.
                await self._verified_remove_container(container_name)
                return
            await self._stop_container_and_client(
                container_name,
                client_process,
            )

        cleanup_task = asyncio.create_task(asyncio.wait_for(cleanup(), timeout=45))
        cancellation_interrupted_cleanup = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancellation_interrupted_cleanup = True
                continue
        await cleanup_task
        if cancellation_interrupted_cleanup:
            raise asyncio.CancelledError

    async def _finish_cleanup_uninterruptibly(
        self,
        container_name: str,
        client_process: asyncio.subprocess.Process,
    ) -> None:
        """Complete bounded cleanup even if this task is cancelled repeatedly."""
        cleanup = asyncio.create_task(
            asyncio.wait_for(
                self._stop_container_and_client(container_name, client_process),
                timeout=45,
            )
        )
        cancellation_interrupted_cleanup = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # The caller's original cancellation is re-raised after this
                # helper returns. Suppress only repeated interrupts while the
                # independently scheduled cleanup task finishes.
                cancellation_interrupted_cleanup = True
                continue
        await cleanup
        if cancellation_interrupted_cleanup:
            raise asyncio.CancelledError

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
        await self._verified_remove_container(container_name)

    async def _verified_remove_container(self, container_name: str) -> None:
        """Force-remove a named sandbox and prove that it is absent."""
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
                _out, error = await asyncio.wait_for(process.communicate(), timeout=30)
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
