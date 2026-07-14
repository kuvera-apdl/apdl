"""Hardened Docker boundary for the real codegen evaluation candidate."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Mapping

from app.editor.environment import CODEGEN_BEHAVIOR_ENV, MODEL_PROVIDER_ENV
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
        network: str | None = None,
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
        self._network = (
            network
            if network is not None
            else os.getenv("CODEGEN_EVALUATION_NETWORK", "").strip()
        )
        if self._network in {"host", "container"}:
            raise ValueError("evaluation network cannot share a host or container namespace")
        self._protected_values = tuple(
            value
            for key, value in self._environment.items()
            if key in MODEL_PROVIDER_ENV and len(value) >= 8
        )
        self._image_revision_validated = False

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
        for key in (*MODEL_PROVIDER_ENV, *CODEGEN_BEHAVIOR_ENV):
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
            raise ValueError("evaluation workspace must be a materialized Git repository")
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
        ]
        if self._network:
            argv += ["--network", self._network]
        for key in (*CODEGEN_BEHAVIOR_ENV, *MODEL_PROVIDER_ENV):
            if key in self._environment:
                # Docker reads the value from the client environment. Secret
                # values never enter argv or the host process list.
                argv += ["-e", key]
        argv += [
            "--entrypoint",
            "python",
            self._image,
            "-m",
            "app.evaluations.candidate",
        ]
        return argv

    async def execute(self, invocation: EvaluationInvocation) -> EvaluationExecution:
        await self._validate_image_revision()
        container_name = f"apdl-codegen-eval-{uuid.uuid4().hex}"
        payload = public_invocation(invocation).model_dump_json().encode() + b"\n"
        try:
            process = await asyncio.create_subprocess_exec(
                *self._docker_argv(invocation, container_name=container_name),
                env=self._docker_environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise EvaluationExecutorError("Docker evaluation executor could not start") from exc

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
            await self._remove_container(container_name)
            await _terminate_process_tree(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise EvaluationExecutorError("Docker evaluation executor timed out") from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            await self._remove_container(container_name)
            await _terminate_process_tree(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise EvaluationExecutorError("Docker evaluation executor closed its input") from exc
        except asyncio.CancelledError:
            await self._remove_container(container_name)
            await _terminate_process_tree(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise

        await _terminate_process_tree(process)
        if stdout.total_bytes + stderr.total_bytes > self._max_output_bytes:
            raise EvaluationExecutorError("Docker evaluation executor exceeded its output limit")
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
            raise EvaluationExecutorError("Docker evaluation candidate image is unavailable")
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

    async def _remove_container(self, container_name: str) -> None:
        """Best-effort removal of a timed-out/cancelled candidate container."""
        try:
            process = await asyncio.create_subprocess_exec(
                self._docker,
                "rm",
                "-f",
                container_name,
                env=self._docker_environment(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=10)
        except (OSError, TimeoutError):
            return
