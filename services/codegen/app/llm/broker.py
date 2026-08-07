"""Unix-socket broker for project-scoped Codegen LLM authority.

A worker starts with a random, changeset-bound socket capability. A successful
acquire then serializes one attempt-scoped plaintext provider credential to that
worker. The production secret boundary is the worker's attested network policy,
not the lease or this cooperative client's reference lifetime.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    model_validator,
)

from app.llm.contracts import (
    LlmAttemptLease,
    LlmExecutionAuthority,
    Phase,
    PreparedLlmAttempt,
)
from app.llm.broker_directory import OpenBrokerRoot, open_broker_root
from app.store.llm_credentials import ProjectCredentialStore
from app.store.llm_routing import (
    abandon_llm_attempt,
    finish_llm_attempt,
    mark_llm_egress,
    prepare_llm_attempt,
)

MAX_BROKER_MESSAGE_BYTES = 32 * 1024
_BROKER_CHILD = re.compile(r"^[0-9a-f]{24}$")
_BROKER_SOCKET_NAME = "broker.sock"
_TOKENS_PER_MILLION = 1_000_000

TerminalStatus = Literal["succeeded", "failed", "cancelled"]
ErrorClassification = Literal[
    "provider_authentication",
    "provider_permission",
    "provider_rate_limited",
    "provider_timeout",
    "provider_unavailable",
    "provider_invalid_request",
    "provider_safety_block",
    "cancelled",
    "unknown",
]


class LlmBrokerError(RuntimeError):
    """Secret-free broker protocol or authority failure."""


def _round_claimed_cost_usd_micros(numerator: int) -> int:
    """Round a token-rate numerator to the nearest micro-dollar."""
    return (numerator + (_TOKENS_PER_MILLION // 2)) // _TOKENS_PER_MILLION


async def _await_terminal_cleanup(cleanup: asyncio.Task[None]) -> None:
    """Keep a terminalization task live through repeated caller cancellation."""
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
        except Exception:
            return
    try:
        cleanup.result()
    except BaseException:
        return


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _AcquireRequest(_StrictModel):
    schema_version: Literal["codegen_llm_broker_request@2"]
    action: Literal["acquire"]
    token: StrictStr = Field(min_length=43, max_length=128, repr=False)
    changeset_id: StrictStr = Field(min_length=1, max_length=128)
    phase: Phase


class _FinishRequest(_StrictModel):
    schema_version: Literal["codegen_llm_broker_request@2"]
    action: Literal["finish"]
    token: StrictStr = Field(min_length=43, max_length=128, repr=False)
    changeset_id: StrictStr = Field(min_length=1, max_length=128)
    attempt_id: UUID
    status: TerminalStatus
    claimed_latency_ms: int = Field(ge=0)
    claimed_input_tokens: int | None = Field(
        default=None, ge=0, le=10_000_000_000
    )
    claimed_output_tokens: int | None = Field(
        default=None, ge=0, le=10_000_000_000
    )
    error_classification: ErrorClassification | None

    @model_validator(mode="after")
    def require_complete_usage(self) -> _FinishRequest:
        if (self.claimed_input_tokens is None) != (
            self.claimed_output_tokens is None
        ):
            raise ValueError(
                "claimed input and output token usage must be supplied together"
            )
        return self


class _SuccessResponse(_StrictModel):
    schema_version: Literal["codegen_llm_broker_response@1"] = (
        "codegen_llm_broker_response@1"
    )
    ok: Literal[True] = True
    lease: LlmAttemptLease | None = None


class _ErrorResponse(_StrictModel):
    schema_version: Literal["codegen_llm_broker_response@1"] = (
        "codegen_llm_broker_response@1"
    )
    ok: Literal[False] = False
    code: Literal[
        "invalid_request",
        "unauthorized",
        "authority_unavailable",
        "attempt_conflict",
    ]


def _strict_json(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > MAX_BROKER_MESSAGE_BYTES:
        raise ValueError("invalid broker message size")

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_nonfinite(_value: str) -> None:
        raise ValueError("non-finite value")

    value = json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=pairs,
        parse_constant=reject_nonfinite,
    )
    if not isinstance(value, dict):
        raise ValueError("broker message must be an object")
    return value


class LlmAttemptBroker:
    """Controller-side authority endpoint scoped to one changeset."""

    def __init__(
        self,
        *,
        pool: Any,
        credential_store: ProjectCredentialStore,
        changeset_id: str,
        socket_path: Path,
        token: str,
        allowed_phases: tuple[Phase, ...],
    ) -> None:
        self._pool = pool
        self._credentials = credential_store
        self._changeset_id = changeset_id
        self._socket_path = socket_path
        self._token = token
        self._allowed_phases = frozenset(allowed_phases)
        self._server: asyncio.AbstractServer | None = None
        self._active: dict[UUID, PreparedLlmAttempt] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._handlers: set[asyncio.Task[None]] = set()
        self._root: OpenBrokerRoot | None = None
        self._child_descriptor: int | None = None
        self._child_name: str | None = None

    async def start(self) -> None:
        if self._closing or self._server is not None:
            raise RuntimeError("LLM broker cannot be started in its current state")
        child_name = self._socket_path.parent.name
        root_path = self._socket_path.parent.parent
        if (
            self._socket_path.name != _BROKER_SOCKET_NAME
            or _BROKER_CHILD.fullmatch(child_name) is None
            or root_path == self._socket_path.parent
        ):
            raise RuntimeError("LLM broker socket path is not canonical")

        root = open_broker_root(root_path, create=False)
        child_descriptor: int | None = None
        server: asyncio.AbstractServer | None = None
        try:
            try:
                os.mkdir(child_name, mode=0o700, dir_fd=root.descriptor)
            except FileExistsError as exc:
                raise RuntimeError(
                    "LLM broker runtime directory already exists"
                ) from exc
            child_descriptor = os.open(
                child_name,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=root.descriptor,
            )
            child_metadata = os.fstat(child_descriptor)
            if (
                not stat.S_ISDIR(child_metadata.st_mode)
                or child_metadata.st_uid != os.geteuid()
            ):
                raise RuntimeError(
                    "Created LLM broker runtime directory is not controller-owned"
                )
            os.fchmod(child_descriptor, 0o711)

            # Bind through the open child descriptor. A concurrent swap of the
            # configured lexical parent/root cannot redirect socket creation.
            child_descriptor_path = root.descriptor_path() / child_name
            server = await asyncio.start_unix_server(
                self._handle,
                path=str(child_descriptor_path / _BROKER_SOCKET_NAME),
                limit=MAX_BROKER_MESSAGE_BYTES + 1,
            )
            socket_metadata = os.stat(
                _BROKER_SOCKET_NAME,
                dir_fd=child_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISSOCK(socket_metadata.st_mode)
                or socket_metadata.st_uid != os.geteuid()
            ):
                raise RuntimeError(
                    "Created LLM broker socket is not controller-owned"
                )
            # The random token is the authority boundary. World-connectable
            # socket permissions let the fixed UID 1000 worker reach a
            # host-user-owned bind mount without exposing a credential to an
            # unauthenticated peer.
            os.chmod(
                _BROKER_SOCKET_NAME,
                0o666,
                dir_fd=child_descriptor,
                follow_symlinks=False,
            )
        except BaseException:
            if server is not None:
                server.close()
                await server.wait_closed()
            if child_descriptor is not None:
                try:
                    os.unlink(
                        _BROKER_SOCKET_NAME,
                        dir_fd=child_descriptor,
                    )
                except FileNotFoundError:
                    pass
                os.close(child_descriptor)
            try:
                os.rmdir(child_name, dir_fd=root.descriptor)
            except OSError:
                pass
            root.close()
            raise

        self._root = root
        self._child_descriptor = child_descriptor
        self._child_name = child_name
        self._server = server

    async def close(
        self,
        *,
        cancelled: bool = False,
    ) -> None:
        self._closing = True
        server = self._server
        if server is not None:
            server.close()
            await server.wait_closed()
            self._server = None
        # ``wait_closed`` stops new accepts but does not await already-created
        # client callback tasks. Give accepted callbacks one scheduling turn,
        # then drain the bounded handlers before clearing their attempts.
        await asyncio.sleep(0)
        current = asyncio.current_task()
        while handlers := tuple(
            task
            for task in self._handlers
            if task is not current and not task.done()
        ):
            await asyncio.gather(*handlers, return_exceptions=True)
        async with self._lock:
            dangling = tuple(self._active.values())
            self._active.clear()
        for attempt in dangling:
            try:
                await abandon_llm_attempt(
                    self._pool,
                    attempt_id=attempt.attempt_id,
                    cancelled=cancelled,
                )
            except Exception:
                # A client may have terminalized the row immediately before the
                # socket closed. Durable transition constraints remain final.
                pass
        child_descriptor = self._child_descriptor
        root = self._root
        child_name = self._child_name
        self._child_descriptor = None
        self._root = None
        self._child_name = None
        if child_descriptor is not None:
            try:
                os.unlink(
                    _BROKER_SOCKET_NAME,
                    dir_fd=child_descriptor,
                )
            except FileNotFoundError:
                pass
            finally:
                os.close(child_descriptor)
        if root is not None:
            try:
                if child_name is not None:
                    os.rmdir(child_name, dir_fd=root.descriptor)
            except OSError:
                pass
            finally:
                root.close()
        self._token = ""

    def _authorized(self, token: str, changeset_id: str) -> bool:
        return secrets.compare_digest(token, self._token) and secrets.compare_digest(
            changeset_id,
            self._changeset_id,
        )

    async def _acquire(self, request: _AcquireRequest) -> LlmAttemptLease:
        if request.phase not in self._allowed_phases:
            raise LlmBrokerError("LLM phase is not authorized for this execution")
        if self._closing:
            raise LlmBrokerError("LLM authority broker is closing")
        attempt: PreparedLlmAttempt | None = None
        registered = False
        try:
            attempt = await prepare_llm_attempt(
                self._pool,
                self._credentials,
                changeset_id=self._changeset_id,
                phase=request.phase,
            )
            async with self._lock:
                if self._closing:
                    raise LlmBrokerError("LLM authority broker is closing")
                self._active[attempt.attempt_id] = attempt
                registered = True
            await mark_llm_egress(self._pool, attempt=attempt)
        except BaseException as exc:
            if attempt is not None:
                cleanup = asyncio.create_task(
                    abandon_llm_attempt(
                        self._pool,
                        attempt_id=attempt.attempt_id,
                        cancelled=isinstance(exc, asyncio.CancelledError),
                    )
                )
                if registered:
                    cleanup.add_done_callback(
                        lambda _task: self._active.pop(
                            attempt.attempt_id, None
                        )
                    )
                await _await_terminal_cleanup(cleanup)
            raise
        return LlmAttemptLease(
            attempt_id=attempt.attempt_id,
            phase=request.phase,
            binding=attempt.binding,
        )

    async def _finish(self, request: _FinishRequest) -> None:
        if request.status == "succeeded" and request.error_classification is not None:
            raise LlmBrokerError("successful attempt cannot contain an error")
        if request.status != "succeeded" and request.error_classification is None:
            raise LlmBrokerError("failed attempt requires an error classification")
        async with self._lock:
            attempt = self._active.get(request.attempt_id)
        if attempt is None:
            raise LlmBrokerError("LLM attempt is not active")
        claimed_cost_usd_micros: int | None = None
        if (
            request.claimed_input_tokens is not None
            and request.claimed_output_tokens is not None
        ):
            numerator = (
                request.claimed_input_tokens
                * attempt.binding.input_cost_per_million_tokens_usd_micros
                + request.claimed_output_tokens
                * attempt.binding.output_cost_per_million_tokens_usd_micros
            )
            claimed_cost_usd_micros = _round_claimed_cost_usd_micros(numerator)
        await finish_llm_attempt(
            self._pool,
            attempt_id=attempt.attempt_id,
            status=request.status,
            claimed_latency_ms=request.claimed_latency_ms,
            claimed_input_tokens=request.claimed_input_tokens,
            claimed_output_tokens=request.claimed_output_tokens,
            claimed_cost_usd_micros=claimed_cost_usd_micros,
            error_classification=request.error_classification,
        )
        async with self._lock:
            self._active.pop(request.attempt_id, None)

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        response: _SuccessResponse | _ErrorResponse
        try:
            if self._closing:
                raise LlmBrokerError("LLM authority broker is closing")
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not raw.endswith(b"\n") or len(raw) > MAX_BROKER_MESSAGE_BYTES:
                raise ValueError("invalid broker message framing")
            payload = _strict_json(raw[:-1])
            action = payload.get("action")
            if action == "acquire":
                request: _AcquireRequest | _FinishRequest = (
                    _AcquireRequest.model_validate_json(raw[:-1])
                )
            elif action == "finish":
                request = _FinishRequest.model_validate_json(raw[:-1])
            else:
                raise ValueError("invalid broker action")
            if not self._authorized(request.token, request.changeset_id):
                response = _ErrorResponse(code="unauthorized")
            elif isinstance(request, _AcquireRequest):
                lease = await self._acquire(request)
                response = _SuccessResponse(lease=lease)
            else:
                await self._finish(request)
                response = _SuccessResponse()
        except (ValueError, UnicodeDecodeError, ValidationError):
            response = _ErrorResponse(code="invalid_request")
        except LlmBrokerError:
            response = _ErrorResponse(code="attempt_conflict")
        except Exception:
            response = _ErrorResponse(code="authority_unavailable")
        try:
            writer.write(response.model_dump_json().encode("utf-8") + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            if task is not None:
                self._handlers.discard(task)


class LlmBrokerClient:
    """Cooperative client that does not cache leases between phase attempts."""

    def __init__(self, authority: LlmExecutionAuthority, changeset_id: str) -> None:
        self._authority = authority
        self._changeset_id = changeset_id

    async def _exchange(self, payload: dict[str, object]) -> _SuccessResponse:
        try:
            reader, writer = await asyncio.open_unix_connection(
                self._authority.socket_path,
                limit=MAX_BROKER_MESSAGE_BYTES + 1,
            )
            try:
                encoded = json.dumps(
                    payload,
                    allow_nan=False,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                if len(encoded) > MAX_BROKER_MESSAGE_BYTES - 1:
                    raise LlmBrokerError("LLM broker request is too large")
                writer.write(encoded + b"\n")
                await writer.drain()
                raw = await reader.readline()
            finally:
                writer.close()
                await writer.wait_closed()
        except (OSError, asyncio.IncompleteReadError) as exc:
            raise LlmBrokerError("LLM authority broker is unavailable") from exc
        try:
            encoded_response = raw[:-1] if raw.endswith(b"\n") else raw
            response = _strict_json(encoded_response)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LlmBrokerError("LLM authority broker returned an invalid response") from exc
        try:
            if response.get("ok") is True:
                return _SuccessResponse.model_validate_json(encoded_response)
            if response.get("ok") is False:
                _ErrorResponse.model_validate_json(encoded_response)
            else:
                raise ValueError("broker response has no canonical result")
        except (ValueError, ValidationError) as exc:
            raise LlmBrokerError(
                "LLM authority broker returned an invalid response"
            ) from exc
        if response.get("ok") is not True:
            raise LlmBrokerError("LLM phase authority is unavailable")
        raise LlmBrokerError("LLM authority broker returned an invalid response")

    async def acquire(self, phase: Phase) -> tuple[LlmAttemptLease, float]:
        response = await self._exchange(
            {
                "schema_version": "codegen_llm_broker_request@2",
                "action": "acquire",
                "token": self._authority.token,
                "changeset_id": self._changeset_id,
                "phase": phase,
            }
        )
        if response.lease is None:
            raise LlmBrokerError("LLM authority broker returned an invalid lease")
        return response.lease, time.monotonic()

    async def finish(
        self,
        lease: LlmAttemptLease,
        started_at: float,
        *,
        status: TerminalStatus,
        error_classification: ErrorClassification | None,
        claimed_input_tokens: int | None = None,
        claimed_output_tokens: int | None = None,
    ) -> None:
        claimed_latency_ms = max(
            0, round((time.monotonic() - started_at) * 1000)
        )
        response = await self._exchange(
            {
                "schema_version": "codegen_llm_broker_request@2",
                "action": "finish",
                "token": self._authority.token,
                "changeset_id": self._changeset_id,
                "attempt_id": str(lease.attempt_id),
                "status": status,
                "claimed_latency_ms": claimed_latency_ms,
                "claimed_input_tokens": claimed_input_tokens,
                "claimed_output_tokens": claimed_output_tokens,
                "error_classification": error_classification,
            }
        )
        if response.lease is not None:
            raise LlmBrokerError("LLM authority broker returned an invalid response")


def classify_provider_failure(exc: BaseException) -> ErrorClassification:
    """Map provider failures without inspecting or persisting their message."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "provider_timeout"
    status_code = getattr(exc, "status_code", None)
    if status_code == 401:
        return "provider_authentication"
    if status_code == 403:
        return "provider_permission"
    if status_code == 429:
        return "provider_rate_limited"
    if status_code == 400:
        return "provider_invalid_request"
    lowered = type(exc).__name__.lower()
    if "authentication" in lowered or "unauthorized" in lowered:
        return "provider_authentication"
    if "permission" in lowered or "forbidden" in lowered:
        return "provider_permission"
    if "ratelimit" in lowered:
        return "provider_rate_limited"
    if "timeout" in lowered:
        return "provider_timeout"
    return "provider_unavailable"
