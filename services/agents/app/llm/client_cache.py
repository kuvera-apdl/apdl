"""Bounded, application-owned provider SDK client lifecycle.

Provider credentials are immutable within one credential version, so clients may
reuse their connection pools only while every part of that exact transport
identity matches.  Cache keys intentionally exclude plaintext API keys.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast, get_args
from uuid import UUID

import anthropic
import httpx
import openai
from google import genai
from google.genai import types as genai_types

from app.llm.contracts import ProviderName


_PROVIDERS = frozenset(get_args(ProviderName))
_PROJECT_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")

logger = logging.getLogger(__name__)


async def _wait_for_cleanup(task: asyncio.Task[Any]) -> Any:
    """Finish one cleanup task before propagating caller cancellation."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                return task.result()
            cancelled = True
            if task.done():
                result = task.result()
                break
    if cancelled:
        raise asyncio.CancelledError
    return result


class ProviderClientCacheClosedError(RuntimeError):
    """The application-owned cache is shutting down or already closed."""


class ProviderClientRetiredError(RuntimeError):
    """The requested identity was retired while its client was being created."""


@dataclass(frozen=True)
class ProviderClientIdentity:
    """Exact non-secret identity of one reusable provider transport."""

    project_id: str
    provider: ProviderName
    endpoint_url: str
    credential_id: UUID | None
    credential_version: int | None
    connection_version: int

    def __post_init__(self) -> None:
        if _PROJECT_ID.fullmatch(self.project_id) is None:
            raise ValueError("project_id must match ^[A-Za-z0-9]{1,64}$")
        if self.provider not in _PROVIDERS:
            raise ValueError(
                "provider must be openai, anthropic, google, xai, or local"
            )
        if (
            not isinstance(self.connection_version, int)
            or isinstance(self.connection_version, bool)
            or self.connection_version < 1
        ):
            raise ValueError("connection_version must be a positive integer")

        normalized_endpoint = _normalize_endpoint_url(self.endpoint_url)
        object.__setattr__(self, "endpoint_url", normalized_endpoint)

        if self.provider == "local":
            if self.credential_id is not None or self.credential_version is not None:
                raise ValueError("local provider identity must not contain a credential")
            return
        if self.credential_id is None:
            raise ValueError("remote provider identity requires credential_id")
        if (
            not isinstance(self.credential_version, int)
            or isinstance(self.credential_version, bool)
            or self.credential_version < 1
        ):
            raise ValueError(
                "remote provider identity requires a positive credential_version"
            )

    @property
    def scope(self) -> tuple[str, ProviderName]:
        """Project/provider scope that can have only one current identity."""
        return self.project_id, self.provider


@dataclass(frozen=True)
class OwnedProviderClient:
    """One SDK client paired with the exact closer that owns its resources."""

    client: Any = field(repr=False)
    close: Callable[[], Awaitable[None]] = field(repr=False)


ProviderClientFactory = Callable[
    [ProviderClientIdentity, str | None],
    OwnedProviderClient | Awaitable[OwnedProviderClient],
]


@dataclass
class _Entry:
    identity: ProviderClientIdentity
    sequence: int
    users: int = 1
    owned: OwnedProviderClient | None = None
    retiring: bool = False
    closing: bool = False


class ProviderClientLease:
    """Reference-counted right to use one cached client."""

    def __init__(self, cache: ProviderClientCache, entry: _Entry) -> None:
        self._cache = cache
        self._entry = entry
        self._released = False
        self._release_task: asyncio.Task[None] | None = None

    @property
    def identity(self) -> ProviderClientIdentity:
        return self._entry.identity

    @property
    def client(self) -> Any:
        if self._released:
            raise RuntimeError("provider client lease has been released")
        owned = self._entry.owned
        if owned is None:
            raise RuntimeError("provider client lease is not ready")
        return owned.client

    async def retire(self) -> None:
        """Prevent new leases and close this client after its last user exits."""
        if not self._released:
            task = asyncio.create_task(self._cache.retire(self.identity))
            await _wait_for_cleanup(task)

    async def release(self) -> None:
        if self._released:
            return
        if self._release_task is None:
            self._release_task = asyncio.create_task(
                self._cache._release(self._entry)
            )
        try:
            await _wait_for_cleanup(self._release_task)
        finally:
            if (
                self._release_task.done()
                and not self._release_task.cancelled()
                and self._release_task.exception() is None
            ):
                self._released = True

    async def __aenter__(self) -> Any:
        return self.client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        del exc_type, exc, traceback
        await self.release()


class ProviderClientCache:
    """Hard-bounded LRU of provider clients protected by async leases.

    The total slot count includes clients being created or closed. When every
    slot is leased, a new identity waits for an idle client instead of exceeding
    the configured bound or closing a client that is in use.
    """

    def __init__(
        self,
        *,
        max_entries: int,
        factory: ProviderClientFactory,
    ) -> None:
        if (
            not isinstance(max_entries, int)
            or isinstance(max_entries, bool)
            or max_entries < 1
        ):
            raise ValueError("max_entries must be a positive integer")
        self._max_entries = max_entries
        self._factory = factory
        self._condition = asyncio.Condition()
        self._entries: dict[ProviderClientIdentity, _Entry] = {}
        self._occupied_slots = 0
        self._sequence = 0
        self._closed = False

    @property
    def max_entries(self) -> int:
        return self._max_entries

    @property
    def entry_count(self) -> int:
        """Number of live, creating, or closing client slots."""
        return self._occupied_slots

    @property
    def closed(self) -> bool:
        return self._closed

    async def acquire(
        self,
        identity: ProviderClientIdentity,
        api_key: str | None,
    ) -> ProviderClientLease:
        """Acquire one exact client, waiting safely when the hard bound is full."""
        _validate_api_key(identity, api_key)

        while True:
            entry_to_close: _Entry | None = None
            reserved: _Entry | None = None
            async with self._condition:
                if self._closed:
                    raise ProviderClientCacheClosedError(
                        "provider client cache is closed"
                    )

                current = self._entries.get(identity)
                if current is not None and current.retiring:
                    await self._condition.wait_for(
                        lambda: self._closed
                        or self._entries.get(identity) is not current
                    )
                    continue
                if current is not None:
                    if current.owned is None:
                        await self._condition.wait_for(
                            lambda: self._closed
                            or self._entries.get(identity) is not current
                            or current.owned is not None
                            or current.retiring
                        )
                        continue
                    current.users += 1
                    current.sequence = self._next_sequence_locked()
                    return ProviderClientLease(self, current)

                self._assert_not_superseded_locked(identity)
                entry_to_close = self._retire_idle_conflict_locked(identity)
                if entry_to_close is None and self._occupied_slots >= self._max_entries:
                    entry_to_close = self._oldest_idle_entry_locked()

                if entry_to_close is None and self._occupied_slots >= self._max_entries:
                    await self._condition.wait()
                    continue

                if entry_to_close is None:
                    reserved = _Entry(
                        identity=identity,
                        sequence=self._next_sequence_locked(),
                    )
                    self._entries[identity] = reserved
                    self._occupied_slots += 1

            if entry_to_close is not None:
                await self._close_detached_entry(entry_to_close)
                continue
            if reserved is not None:
                return await self._create_reserved(reserved, api_key)

    async def retire(self, identity: ProviderClientIdentity) -> None:
        """Retire an identity and close it now or after its final lease."""
        entry_to_close: _Entry | None = None
        async with self._condition:
            entry = self._entries.get(identity)
            if entry is None:
                return
            entry.retiring = True
            if entry.owned is not None and entry.users == 0:
                entry_to_close = self._detach_locked(entry)
            self._condition.notify_all()
        if entry_to_close is not None:
            await self._close_detached_entry(entry_to_close)

    async def aclose(self) -> None:
        """Drain every lease and deterministically close all owned clients."""
        async with self._condition:
            self._closed = True
            for entry in self._entries.values():
                entry.retiring = True
            self._condition.notify_all()

        while True:
            entry_to_close: _Entry | None = None
            async with self._condition:
                if self._occupied_slots == 0:
                    return
                entry_to_close = next(
                    (
                        self._detach_locked(entry)
                        for entry in tuple(self._entries.values())
                        if entry.owned is not None
                        and entry.users == 0
                        and not entry.closing
                    ),
                    None,
                )
                if entry_to_close is None:
                    await self._condition.wait()
                    continue
            await self._close_detached_entry(entry_to_close)

    async def _create_reserved(
        self,
        entry: _Entry,
        api_key: str | None,
    ) -> ProviderClientLease:
        owned: OwnedProviderClient | None = None
        try:
            created = self._factory(entry.identity, api_key)
            candidate = await created if inspect.isawaitable(created) else created
            if not isinstance(candidate, OwnedProviderClient):
                raise TypeError("provider client factory must return OwnedProviderClient")
            owned = candidate

            close_after_creation = False
            async with self._condition:
                entry.owned = owned
                if self._closed or entry.retiring:
                    entry.users = 0
                    self._detach_locked(entry)
                    close_after_creation = True
                else:
                    entry.sequence = self._next_sequence_locked()
                self._condition.notify_all()
        except BaseException:
            cleanup = asyncio.create_task(self._abandon_reserved(entry, owned))
            await _wait_for_cleanup(cleanup)
            raise

        if close_after_creation:
            await self._close_detached_entry(entry)
            if self._closed:
                raise ProviderClientCacheClosedError(
                    "provider client cache closed during creation"
                )
            raise ProviderClientRetiredError(
                "provider client identity retired during creation"
            )
        return ProviderClientLease(self, entry)

    async def _abandon_reserved(
        self,
        entry: _Entry,
        owned: OwnedProviderClient | None,
    ) -> None:
        entry_to_close: _Entry | None = None
        async with self._condition:
            if owned is None:
                if self._entries.get(entry.identity) is entry:
                    self._entries.pop(entry.identity)
                self._occupied_slots -= 1
            else:
                entry.owned = owned
                entry.users = 0
                entry_to_close = self._detach_locked(entry)
            self._condition.notify_all()
        if entry_to_close is not None:
            await self._close_detached_entry(entry_to_close)

    async def _release(self, entry: _Entry) -> None:
        entry_to_close: _Entry | None = None
        async with self._condition:
            if entry.users < 1:
                raise RuntimeError("provider client lease reference count underflow")
            entry.users -= 1
            entry.sequence = self._next_sequence_locked()
            if entry.users == 0 and (entry.retiring or self._closed):
                entry_to_close = self._detach_locked(entry)
            self._condition.notify_all()
        if entry_to_close is not None:
            await self._close_detached_entry(entry_to_close)

    def _retire_idle_conflict_locked(
        self,
        identity: ProviderClientIdentity,
    ) -> _Entry | None:
        for entry in tuple(self._entries.values()):
            if entry.identity.scope != identity.scope or entry.identity == identity:
                continue
            entry.retiring = True
            if entry.owned is not None and entry.users == 0:
                return self._detach_locked(entry)
        return None

    def _assert_not_superseded_locked(
        self,
        identity: ProviderClientIdentity,
    ) -> None:
        if any(
            entry.identity.scope == identity.scope
            and entry.identity.connection_version >= identity.connection_version
            for entry in self._entries.values()
        ):
            raise ProviderClientRetiredError(
                "provider client identity was superseded by a current version"
            )

    def _oldest_idle_entry_locked(self) -> _Entry | None:
        idle = [
            entry
            for entry in self._entries.values()
            if entry.owned is not None and entry.users == 0 and not entry.closing
        ]
        if not idle:
            return None
        return self._detach_locked(min(idle, key=lambda item: item.sequence))

    def _detach_locked(self, entry: _Entry) -> _Entry:
        if entry.closing:
            raise RuntimeError("provider client entry is already closing")
        if entry.owned is None or entry.users != 0:
            raise RuntimeError("only idle initialized provider clients can close")
        entry.closing = True
        if self._entries.get(entry.identity) is entry:
            self._entries.pop(entry.identity)
        return entry

    async def _close_detached_entry(self, entry: _Entry) -> None:
        cleanup = asyncio.create_task(self._close_and_release_slot(entry))
        await _wait_for_cleanup(cleanup)

    async def _close_and_release_slot(self, entry: _Entry) -> None:
        owned = entry.owned
        if owned is None:
            raise RuntimeError("cannot close an uninitialized provider client")
        try:
            await owned.close()
        except Exception as exc:
            logger.error(
                "Failed to close provider client "
                "(project=%s, provider=%s, exception_type=%s)",
                entry.identity.project_id,
                entry.identity.provider,
                type(exc).__name__,
            )
        finally:
            async with self._condition:
                self._occupied_slots -= 1
                self._condition.notify_all()

    def _next_sequence_locked(self) -> int:
        self._sequence += 1
        return self._sequence


def _normalize_endpoint_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    try:
        parsed = httpx.URL(raw)
    except Exception as exc:
        raise ValueError("provider endpoint is not a valid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise ValueError("provider endpoint must be an absolute HTTP(S) URL")
    if parsed.userinfo:
        raise ValueError("provider endpoint must not contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError("provider endpoint must not contain query or fragment data")
    return str(parsed).rstrip("/")


def _validate_api_key(
    identity: ProviderClientIdentity,
    api_key: str | None,
) -> None:
    if identity.provider == "local":
        if api_key is not None:
            raise ValueError("local provider must not receive an API key")
        return
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("remote provider requires a non-empty API key")


async def build_provider_client(
    identity: ProviderClientIdentity,
    api_key: str | None,
    *,
    request_timeout_seconds: float = 120.0,
) -> OwnedProviderClient:
    """Construct the production SDK client and its exact resource closer."""
    _validate_api_key(identity, api_key)
    provider = identity.provider
    endpoint_url = identity.endpoint_url

    if provider in {"openai", "xai", "local"}:
        client = openai.AsyncOpenAI(
            api_key="local" if provider == "local" else cast(str, api_key),
            base_url=endpoint_url,
            timeout=request_timeout_seconds,
        )

        async def close_openai() -> None:
            await client.close()

        return OwnedProviderClient(client, close_openai)

    if provider == "anthropic":
        anthropic_client = anthropic.AsyncAnthropic(
            api_key=cast(str, api_key),
            base_url=endpoint_url,
            timeout=request_timeout_seconds,
        )

        async def close_anthropic() -> None:
            await anthropic_client.close()

        return OwnedProviderClient(anthropic_client, close_anthropic)

    google_client = genai.Client(
        api_key=cast(str, api_key),
        http_options=genai_types.HttpOptions(
            base_url=endpoint_url,
            timeout=int(request_timeout_seconds * 1000),
        ),
    )

    async def close_google() -> None:
        try:
            await google_client.aio.aclose()
        finally:
            google_client.close()

    return OwnedProviderClient(google_client, close_google)
