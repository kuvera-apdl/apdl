"""Hardened Docker boundary for the real codegen evaluation candidate."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from collections.abc import Mapping

from app.editor.environment import (
    CODEGEN_BEHAVIOR_ENV,
    MODEL_PROVIDER_CREDENTIAL_ENV,
    MODEL_PROVIDER_ENV,
)
from app.egress import (
    EGRESS_PROXY_ENV,
    EgressPolicyAttestation,
    attest_docker_egress_policy,
    proxy_environment,
    relay_command,
    validate_policy_sha256,
    validate_proxy_image_id,
    validate_socket_volume,
    worker_socket_mount,
)
from app.evaluations.json_io import parse_strict_json_object
from app.evaluations.models import EvaluationExecution
from app.evaluations.runner import EvaluationInvocation
from app.evaluations.subprocess_executor import (
    EvaluationExecutorError,
    _contains_protected_output,
    _read_bounded,
    _terminate_process_tree,
    public_invocation,
    sanitized_evaluation_environment,
)

logger = logging.getLogger(__name__)


class DockerEvaluationExecutor:
    """Run one candidate per container with only its fixture mounted read/write."""

    def __init__(
        self,
        *,
        image: str = "apdl-codegen-sandbox:latest",
        docker_bin: str = "docker",
        timeout_seconds: float | None = None,
        max_output_bytes: int = 1_000_000,
        environment: Mapping[str, str] | None = None,
        proxy_url: str | None = None,
        probe_image: str = "",
        egress_policy_sha256: str = "",
        egress_proxy_image_id: str = "",
        egress_socket_volume: str = "",
    ) -> None:
        if not image.strip() or "\x00" in image:
            raise ValueError("evaluation image must be a non-empty Docker reference")
        if not re.search(r"(?:@|^)sha256:[0-9a-f]{64}$", image):
            raise ValueError(
                "evaluation image must be immutable (sha256 image ID or digest)"
            )
        if not docker_bin.strip() or "\x00" in docker_bin:
            raise ValueError("docker executable must be non-empty")
        if timeout_seconds is None:
            # Lazy import avoids the config -> evaluation-models package import
            # cycle during application startup.
            from app.config import codegen_job_budget

            timeout_seconds = float(codegen_job_budget())
        resolved_timeout = float(timeout_seconds)
        if resolved_timeout <= 0:
            raise ValueError("evaluation timeout must be positive")
        if max_output_bytes < 1024:
            raise ValueError("evaluation output limit must be at least 1024 bytes")
        self._image = image
        self._docker = docker_bin
        self._timeout_seconds = resolved_timeout
        self._max_output_bytes = max_output_bytes
        self._environment = sanitized_evaluation_environment(environment)
        self._probe_image = validate_proxy_image_id(probe_image)
        self._egress_policy_sha256 = validate_policy_sha256(egress_policy_sha256)
        self._egress_proxy_image_id = validate_proxy_image_id(egress_proxy_image_id)
        self._egress_socket_volume = validate_socket_volume(egress_socket_volume)
        self._proxy_environment = (
            proxy_environment(proxy_url)
            if proxy_url is not None
            else proxy_environment()
        )
        self._environment.update(self._proxy_environment)
        self._protected_values = tuple(
            value
            for key, value in self._environment.items()
            if key in MODEL_PROVIDER_CREDENTIAL_ENV and len(value) >= 8
        )
        self._image_revision_validated = False
        self._egress_attestations: dict[str, EgressPolicyAttestation] = {}

    def egress_attestation_sha256(self, invocation_id: str) -> str | None:
        """Trusted controller evidence recorded before one candidate launch."""
        attestation = self._egress_attestations.get(invocation_id)
        return attestation.evidence_sha256() if attestation is not None else None

    def _docker_environment(self) -> dict[str, str]:
        """Minimal Docker-client environment; candidate values pass by name."""
        environment = {"PATH": os.environ.get("PATH", os.defpath)}
        source = {**os.environ, **self._environment}
        for key in (
            "HOME",
            "DOCKER_HOST",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        ):
            if key in source:
                environment[key] = source[key]
        for key in (*MODEL_PROVIDER_ENV, *CODEGEN_BEHAVIOR_ENV, *EGRESS_PROXY_ENV):
            if key in self._environment:
                environment[key] = self._environment[key]
        return environment

    def _docker_argv(
        self,
        invocation: EvaluationInvocation,
        *,
        container_name: str | None = None,
    ) -> list[str]:
        workspace = invocation.workspace.resolve()
        if not workspace.is_dir() or not (workspace / ".git").is_dir():
            raise ValueError(
                "evaluation workspace must be a materialized Git repository"
            )
        if "," in str(workspace):
            raise ValueError("evaluation workspace path cannot contain a comma")
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        name = container_name or f"apdl-codegen-eval-{uuid.uuid4().hex}"
        argv = [
            self._docker,
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            os.getenv("CODEGEN_EVALUATION_PIDS", "512"),
            "--memory",
            os.getenv("CODEGEN_EVALUATION_MEMORY", "2g"),
            "--cpus",
            os.getenv("CODEGEN_EVALUATION_CPUS", "2"),
            "--user",
            f"{uid}:{gid}",
            "--workdir",
            "/workspace",
            "--mount",
            f"type=bind,src={workspace},dst=/workspace",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=4g,mode=1777",
            "-e",
            "HOME=/tmp",
            "-e",
            "TMPDIR=/tmp",
            "-e",
            "CODEGEN_WORKDIR=/tmp",
            "-e",
            "APDL_CODEGEN_ISOLATED_WORKER=true",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "--network",
            "none",
            "--mount",
            worker_socket_mount(self._egress_socket_volume),
        ]
        for key in (
            *CODEGEN_BEHAVIOR_ENV,
            *MODEL_PROVIDER_ENV,
            *EGRESS_PROXY_ENV,
        ):
            if key in self._environment:
                # Docker reads the value from the client environment. Secret
                # values never enter argv or the host process list.
                argv += ["-e", key]
        argv += [
            "--entrypoint",
            "python",
            self._image,
            *relay_command(["python", "-m", "app.evaluations.candidate"]),
        ]
        return argv

    async def execute(self, invocation: EvaluationInvocation) -> EvaluationExecution:
        await self._validate_image_revision()
        attestation = await asyncio.to_thread(
            attest_docker_egress_policy,
            docker_bin=self._docker,
            probe_image=self._probe_image,
            launch_id=invocation.invocation_id,
            socket_volume=self._egress_socket_volume,
            expected_policy_sha256=self._egress_policy_sha256,
            expected_proxy_image_id=self._egress_proxy_image_id,
            proxy_url=self._proxy_environment["HTTP_PROXY"],
            environment=self._docker_environment(),
        )
        self._egress_attestations[invocation.invocation_id] = attestation
        container_name = f"apdl-codegen-eval-{uuid.uuid4().hex}"
        payload = public_invocation(invocation).model_dump_json().encode() + b"\n"
        spawn_task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *self._docker_argv(invocation, container_name=container_name),
                env=self._docker_environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        )
        try:
            process = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            try:
                await self._finish_spawn_cleanup_uninterruptibly(
                    container_name,
                    spawn_task,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Evaluation spawn cleanup failed while cancellation was "
                    "pending for %s",
                    container_name,
                )
            raise
        except OSError as exc:
            raise EvaluationExecutorError(
                "Docker evaluation executor could not start"
            ) from exc

        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            _read_bounded(process.stdout, retain_bytes=self._max_output_bytes)
        )
        stderr_task = asyncio.create_task(
            _read_bounded(process.stderr, retain_bytes=self._max_output_bytes)
        )

        async def exchange():
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            await process.wait()
            return await asyncio.gather(stdout_task, stderr_task)

        try:
            stdout, stderr = await asyncio.wait_for(
                exchange(), timeout=self._timeout_seconds
            )
        except TimeoutError as exc:
            await self._finish_cleanup_uninterruptibly(
                container_name,
                process,
                stdout_task,
                stderr_task,
            )
            raise EvaluationExecutorError(
                "Docker evaluation executor timed out"
            ) from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            await self._finish_cleanup_uninterruptibly(
                container_name,
                process,
                stdout_task,
                stderr_task,
            )
            raise EvaluationExecutorError(
                "Docker evaluation executor closed its input"
            ) from exc
        except asyncio.CancelledError:
            try:
                await self._finish_cleanup_uninterruptibly(
                    container_name,
                    process,
                    stdout_task,
                    stderr_task,
                )
            except Exception:
                logger.exception(
                    "Evaluation container cleanup failed while cancellation "
                    "was pending for %s",
                    container_name,
                )
            raise
        except Exception:
            await self._finish_cleanup_uninterruptibly(
                container_name,
                process,
                stdout_task,
                stderr_task,
            )
            raise

        # A completed `docker run --rm` client can still leave its named
        # provider-bearing container alive after a daemon-stream failure.
        # Preserve the captured output, but do not inspect or return it until
        # the client is reaped and container absence is verified.
        await self._finish_cleanup_uninterruptibly(
            container_name,
            process,
            stdout_task,
            stderr_task,
        )
        if stdout.total_bytes + stderr.total_bytes > self._max_output_bytes:
            raise EvaluationExecutorError(
                "Docker evaluation executor exceeded its output limit"
            )
        if process.returncode != 0:
            raise EvaluationExecutorError(
                f"Docker evaluation executor exited with status {process.returncode}"
            )
        try:
            decoded = stdout.data.decode("utf-8", errors="strict")
            decoded_payload = parse_strict_json_object(decoded)
            if _contains_protected_output(decoded_payload, self._protected_values):
                raise EvaluationExecutorError(
                    "Docker evaluation executor output contained protected secret material"
                )
            # Re-encode as JSON so strict Pydantic JSON semantics accept enum
            # strings while still rejecting numeric/bool coercion.
            result = EvaluationExecution.model_validate_json(decoded)
        except EvaluationExecutorError:
            raise
        except (UnicodeDecodeError, ValueError):
            raise EvaluationExecutorError(
                "Docker evaluation executor returned invalid strict JSON"
            ) from None
        if result.invocation_id != invocation.invocation_id:
            raise EvaluationExecutorError(
                "Docker evaluation executor returned a different invocation id"
            )
        return result

    async def _validate_image_revision(self) -> None:
        """Bind the immutable candidate image label to the claimed run revision."""
        if self._image_revision_validated:
            return
        expected = self._environment.get("CODEGEN_REVISION", "").strip()
        if not expected or expected == "development-unversioned":
            raise EvaluationExecutorError(
                "Docker evaluation requires an immutable CODEGEN_REVISION"
            )
        try:
            process = await asyncio.create_subprocess_exec(
                self._docker,
                "image",
                "inspect",
                "--format",
                '{{ index .Config.Labels "dev.apdl.codegen.revision" }}',
                self._image,
                env=self._docker_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        except (OSError, TimeoutError) as exc:
            raise EvaluationExecutorError(
                "Docker evaluation could not inspect the candidate image"
            ) from exc
        if process.returncode != 0:
            raise EvaluationExecutorError(
                "Docker evaluation candidate image is unavailable"
            )
        try:
            observed = stdout.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            raise EvaluationExecutorError(
                "Docker evaluation candidate revision label is invalid"
            ) from None
        if observed != expected:
            raise EvaluationExecutorError(
                "Docker evaluation candidate revision does not match CODEGEN_REVISION"
            )
        self._image_revision_validated = True

    async def _finish_spawn_cleanup_uninterruptibly(
        self,
        container_name: str,
        spawn_task: asyncio.Task[asyncio.subprocess.Process],
    ) -> None:
        """Settle a cancelled candidate spawn and verify its name is absent."""

        async def cleanup() -> None:
            try:
                process = await spawn_task
            except (asyncio.CancelledError, Exception):
                await self._remove_container(container_name)
                return
            await _terminate_process_tree(process)
            await self._remove_container(container_name)

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
        process: asyncio.subprocess.Process,
        stdout_task: asyncio.Task,
        stderr_task: asyncio.Task,
    ) -> None:
        """Bound and shield cleanup from repeated task cancellation."""

        async def cleanup() -> None:
            # Reap the creating Docker CLI before rm, avoiding the race where
            # `docker run` creates the container after an early remove.
            await _terminate_process_tree(process)
            await self._remove_container(container_name)
            await asyncio.gather(
                stdout_task,
                stderr_task,
                return_exceptions=True,
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

    async def _docker_control(self, *args: str) -> tuple[int | None, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                self._docker,
                *args,
                env=self._docker_environment(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=15,
            )
            return (
                process.returncode,
                (stderr or b"").decode("utf-8", "replace")[-400:],
            )
        except (OSError, TimeoutError) as exc:
            return None, str(exc)[-400:]

    async def _remove_container(self, container_name: str) -> None:
        """Force-remove and verify absence of a provider-bearing candidate."""
        last_detail = ""
        for _attempt in range(2):
            remove_rc, remove_detail = await self._docker_control(
                "rm",
                "-f",
                container_name,
            )
            if remove_rc == 0:
                return
            inspect_rc, inspect_detail = await self._docker_control(
                "inspect",
                "--type",
                "container",
                container_name,
            )
            combined = f"{remove_detail}\n{inspect_detail}".strip()
            normalized = combined.casefold()
            if inspect_rc not in (None, 0) and (
                "no such container" in normalized or "no such object" in normalized
            ):
                return
            last_detail = combined
        raise EvaluationExecutorError(
            "could not verify removal of Docker evaluation container: "
            + last_detail[-300:]
        )
