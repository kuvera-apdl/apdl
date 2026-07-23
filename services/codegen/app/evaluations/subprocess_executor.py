"""Credential-minimal, no-shell subprocess boundary for evaluation candidates.

Environment filtering and JSON isolation reduce accidental credential exposure,
but they are not an OS security boundary. Deployments must still run this
executor in a separate credential-minimal worker or container that cannot read
the codegen service process, filesystem, metadata service, or credential store.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from app.evaluations.json_io import parse_strict_json_object
from app.evaluations.models import (
    Ecosystem,
    EvaluationExecution,
    EvaluationTask,
    InvocationId,
    StrictModel,
)
from app.evaluations.runner import EvaluationInvocation
from app.editor.environment import (
    EVALUATION_ENV,
    MODEL_PROVIDER_CREDENTIAL_ENV,
    resolve_model_provider_environment,
)
from app.safety.secrets import structured_value_contains_secret


_ENV_PASSTHROUGH = frozenset(EVALUATION_ENV)

_MODEL_SECRET_ENV = frozenset(MODEL_PROVIDER_CREDENTIAL_ENV)


class PublicEvaluationInvocation(StrictModel):
    """The complete and intentionally small JSON contract visible to a candidate."""

    schema_version: Literal["public_evaluation_invocation@1"] = (
        "public_evaluation_invocation@1"
    )
    invocation_id: InvocationId
    ecosystem: Ecosystem
    task: EvaluationTask


class EvaluationExecutorError(RuntimeError):
    """Safe evaluator failure that never embeds candidate-controlled output."""


@dataclass(frozen=True)
class _BoundedBytes:
    data: bytes
    total_bytes: int


def sanitized_evaluation_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Allow only process basics and model-provider settings into the worker."""
    source = os.environ if source is None else source
    environment = {key: source[key] for key in _ENV_PASSTHROUGH if key in source}
    environment.update(resolve_model_provider_environment(source))
    environment.setdefault("PATH", os.defpath)
    environment["AIDER_ANALYTICS"] = "false"
    environment["AIDER_CHECK_UPDATE"] = "false"
    return environment


def public_invocation(invocation: EvaluationInvocation) -> PublicEvaluationInvocation:
    return PublicEvaluationInvocation(
        invocation_id=invocation.invocation_id,
        ecosystem=invocation.ecosystem,
        task=invocation.task,
    )


async def _read_bounded(
    stream: asyncio.StreamReader,
    *,
    retain_bytes: int,
) -> _BoundedBytes:
    retained = bytearray()
    total = 0
    while chunk := await stream.read(8192):
        total += len(chunk)
        remaining = retain_bytes - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
    return _BoundedBytes(data=bytes(retained), total_bytes=total)


class SubprocessEvaluationExecutor:
    """Execute one opaque invocation via argv and strict stdin/stdout JSON."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = 300,
        max_output_bytes: int = 1_000_000,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not command or any(not item or "\x00" in item for item in command):
            raise ValueError("evaluation executor command must contain non-empty argv")
        if timeout_seconds <= 0:
            raise ValueError("evaluation executor timeout must be positive")
        if max_output_bytes < 1024:
            raise ValueError(
                "evaluation executor output limit must be at least 1024 bytes"
            )
        self._command = tuple(command)
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._environment = sanitized_evaluation_environment(environment)
        self._protected_values = tuple(
            value
            for key, value in self._environment.items()
            if key in _MODEL_SECRET_ENV and len(value) >= 8
        )

    async def execute(self, invocation: EvaluationInvocation) -> EvaluationExecution:
        payload = public_invocation(invocation).model_dump_json().encode() + b"\n"
        worker_boundary = invocation.workspace.parent
        worker_home = worker_boundary / "worker-home"
        worker_tmp = worker_boundary / "worker-tmp"
        worker_config = worker_boundary / "worker-config"
        for directory in (worker_home, worker_tmp, worker_config):
            directory.mkdir(mode=0o700, exist_ok=False)
        environment = dict(self._environment)
        environment.update(
            {
                "HOME": str(worker_home),
                "TMPDIR": str(worker_tmp),
                "XDG_CONFIG_HOME": str(worker_config),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                cwd=invocation.workspace,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise EvaluationExecutorError(
                "evaluation executor could not start"
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

        async def exchange() -> tuple[_BoundedBytes, _BoundedBytes]:
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
            await _terminate_process_tree(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise EvaluationExecutorError("evaluation executor timed out") from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            await _terminate_process_tree(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise EvaluationExecutorError(
                "evaluation executor closed its input"
            ) from exc
        except asyncio.CancelledError:
            await _terminate_process_tree(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise

        # The direct worker has exited, but a candidate may have left detached
        # descendants in the evaluation session. Reap that session before
        # accepting its result.
        await _terminate_process_tree(process)
        if stdout.total_bytes + stderr.total_bytes > self._max_output_bytes:
            raise EvaluationExecutorError(
                "evaluation executor exceeded its output limit"
            )
        if process.returncode != 0:
            raise EvaluationExecutorError(
                f"evaluation executor exited with status {process.returncode}"
            )
        try:
            decoded = stdout.data.decode("utf-8", errors="strict")
            decoded_payload = parse_strict_json_object(decoded)
            if _contains_protected_output(decoded_payload, self._protected_values):
                raise EvaluationExecutorError(
                    "evaluation executor output contained protected secret material"
                )
            result = EvaluationExecution.model_validate_json(decoded)
        except EvaluationExecutorError:
            raise
        except (UnicodeDecodeError, ValueError):
            raise EvaluationExecutorError(
                "evaluation executor returned invalid strict JSON"
            ) from None
        if result.invocation_id != invocation.invocation_id:
            raise EvaluationExecutorError(
                "evaluation executor returned a different invocation id"
            )
        return result


def _contains_protected_output(value, protected_values: tuple[str, ...]) -> bool:
    return structured_value_contains_secret(
        value,
        protected_values=protected_values,
    )


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=0.5)
            except TimeoutError:
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except TimeoutError:
            process.kill()
    if process.returncode is None:
        await process.wait()
