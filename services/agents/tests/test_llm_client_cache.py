"""Application-owned provider SDK client cache contracts."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest

from app.llm import client_cache
from app.llm.client_cache import (
    OwnedProviderClient,
    ProviderClientCache,
    ProviderClientCacheClosedError,
    ProviderClientIdentity,
    ProviderClientRetiredError,
)


@dataclass
class _FakeClient:
    name: str


class _Factory:
    def __init__(self) -> None:
        self.created: list[tuple[ProviderClientIdentity, str | None, _FakeClient]] = []
        self.closed: Counter[str] = Counter()

    async def __call__(
        self,
        identity: ProviderClientIdentity,
        api_key: str | None,
    ) -> OwnedProviderClient:
        value = _FakeClient(f"client-{len(self.created) + 1}")
        self.created.append((identity, api_key, value))

        async def close() -> None:
            self.closed[value.name] += 1

        return OwnedProviderClient(value, close)


def _identity(
    *,
    project_id: str = "projectA",
    provider: str = "openai",
    endpoint_url: str | None = None,
    credential_id: str = "10000000-0000-4000-8000-000000000001",
    credential_version: int = 1,
    connection_version: int = 1,
) -> ProviderClientIdentity:
    if provider == "local":
        return ProviderClientIdentity(
            project_id=project_id,
            provider="local",
            endpoint_url=endpoint_url or "http://local.test:11434/v1/",
            credential_id=None,
            credential_version=None,
            connection_version=connection_version,
        )
    return ProviderClientIdentity(
        project_id=project_id,
        provider=provider,  # type: ignore[arg-type]
        endpoint_url=endpoint_url or f"https://{provider}.example/v1/",
        credential_id=UUID(credential_id),
        credential_version=credential_version,
        connection_version=connection_version,
    )


def test_identity_is_strict_normalized_and_secret_free() -> None:
    identity = _identity(endpoint_url=" https://openai.example/v1/// ")

    assert identity.endpoint_url == "https://openai.example/v1"
    assert "provider-secret" not in repr(identity)

    with pytest.raises(ValueError, match="local provider identity"):
        ProviderClientIdentity(
            project_id="projectA",
            provider="local",
            endpoint_url="http://local.test/v1",
            credential_id=UUID("10000000-0000-4000-8000-000000000001"),
            credential_version=1,
            connection_version=1,
        )
    with pytest.raises(ValueError, match="requires credential_id"):
        ProviderClientIdentity(
            project_id="projectA",
            provider="openai",
            endpoint_url="https://openai.example/v1",
            credential_id=None,
            credential_version=None,
            connection_version=1,
        )


@pytest.mark.asyncio
async def test_same_exact_identity_reuses_one_client_concurrently() -> None:
    factory = _Factory()
    cache = ProviderClientCache(max_entries=2, factory=factory)
    identity = _identity()

    first = await cache.acquire(identity, "provider-secret")
    second = await cache.acquire(identity, "provider-secret")

    assert first.client is second.client
    assert len(factory.created) == 1
    assert cache.entry_count == 1

    await first.release()
    assert factory.closed == Counter()
    await second.release()
    assert factory.closed == Counter()
    await cache.aclose()
    assert factory.closed == Counter({"client-1": 1})


@pytest.mark.asyncio
async def test_concurrent_miss_constructs_one_client() -> None:
    started = asyncio.Event()
    continue_creation = asyncio.Event()
    calls = 0
    close_count = 0

    async def factory(
        identity: ProviderClientIdentity,
        api_key: str | None,
    ) -> OwnedProviderClient:
        nonlocal calls, close_count
        del identity, api_key
        calls += 1
        started.set()
        await continue_creation.wait()
        value = _FakeClient("shared")

        async def close() -> None:
            nonlocal close_count
            close_count += 1

        return OwnedProviderClient(value, close)

    cache = ProviderClientCache(max_entries=1, factory=factory)
    identity = _identity()
    first_task = asyncio.create_task(cache.acquire(identity, "secret"))
    await started.wait()
    second_task = asyncio.create_task(cache.acquire(identity, "secret"))
    await asyncio.sleep(0)
    assert not second_task.done()

    continue_creation.set()
    first, second = await asyncio.gather(first_task, second_task)
    assert first.client is second.client
    assert calls == 1

    await first.release()
    await second.release()
    await cache.aclose()
    assert close_count == 1


@pytest.mark.asyncio
async def test_lru_eviction_closes_only_the_oldest_idle_client() -> None:
    factory = _Factory()
    cache = ProviderClientCache(max_entries=2, factory=factory)
    first_identity = _identity(project_id="projectA")
    second_identity = _identity(project_id="projectB")
    third_identity = _identity(project_id="projectC")

    first = await cache.acquire(first_identity, "secret-a")
    await first.release()
    second = await cache.acquire(second_identity, "secret-b")
    await second.release()

    first_again = await cache.acquire(first_identity, "secret-a")
    await first_again.release()
    third = await cache.acquire(third_identity, "secret-c")

    assert len(factory.created) == 3
    assert factory.closed == Counter({"client-2": 1})
    assert cache.entry_count == 2

    await third.release()
    await cache.aclose()
    assert factory.closed == Counter(
        {"client-1": 1, "client-2": 1, "client-3": 1}
    )


@pytest.mark.asyncio
async def test_hard_bound_waits_instead_of_closing_a_leased_client() -> None:
    factory = _Factory()
    cache = ProviderClientCache(max_entries=1, factory=factory)
    first = await cache.acquire(_identity(project_id="projectA"), "secret-a")

    waiting = asyncio.create_task(
        cache.acquire(_identity(project_id="projectB"), "secret-b")
    )
    await asyncio.sleep(0)

    assert not waiting.done()
    assert len(factory.created) == 1
    assert factory.closed == Counter()

    await first.release()
    second = await asyncio.wait_for(waiting, timeout=1)
    assert factory.closed == Counter({"client-1": 1})
    assert len(factory.created) == 2

    await second.release()
    await cache.aclose()


@pytest.mark.asyncio
async def test_new_scope_identity_retires_old_client_after_its_last_lease() -> None:
    factory = _Factory()
    cache = ProviderClientCache(max_entries=2, factory=factory)
    old_identity = _identity(connection_version=1, credential_version=1)
    new_identity = _identity(
        credential_id="20000000-0000-4000-8000-000000000002",
        credential_version=2,
        connection_version=2,
    )

    old = await cache.acquire(old_identity, "old-secret")
    new = await cache.acquire(new_identity, "new-secret")

    assert factory.closed == Counter()
    assert old.client is not new.client

    await old.release()
    assert factory.closed == Counter({"client-1": 1})
    await new.release()
    await cache.aclose()
    assert factory.closed == Counter({"client-1": 1, "client-2": 1})


@pytest.mark.asyncio
async def test_stale_identity_cannot_retire_a_newer_cached_client() -> None:
    factory = _Factory()
    cache = ProviderClientCache(max_entries=2, factory=factory)
    current_identity = _identity(
        credential_id="20000000-0000-4000-8000-000000000002",
        credential_version=2,
        connection_version=2,
    )
    stale_identity = _identity(credential_version=1, connection_version=1)

    current = await cache.acquire(current_identity, "current-secret")
    current_client = current.client
    await current.release()

    with pytest.raises(ProviderClientRetiredError, match="superseded"):
        await cache.acquire(stale_identity, "stale-secret")

    current_again = await cache.acquire(current_identity, "current-secret")
    assert current_again.client is current_client
    assert len(factory.created) == 1
    assert factory.closed == Counter()

    await current_again.release()
    await cache.aclose()


@pytest.mark.asyncio
async def test_explicit_retirement_closes_idle_and_recreates_on_demand() -> None:
    factory = _Factory()
    cache = ProviderClientCache(max_entries=1, factory=factory)
    identity = _identity()

    first = await cache.acquire(identity, "secret")
    first_client = first.client
    await first.release()
    await cache.retire(identity)

    assert factory.closed == Counter({"client-1": 1})
    assert cache.entry_count == 0

    second = await cache.acquire(identity, "secret")
    assert second.client is not first_client
    await second.release()
    await cache.aclose()


@pytest.mark.asyncio
async def test_retirement_during_creation_closes_new_client_and_fails_lease() -> None:
    started = asyncio.Event()
    continue_creation = asyncio.Event()
    close_count = 0

    async def factory(
        identity: ProviderClientIdentity,
        api_key: str | None,
    ) -> OwnedProviderClient:
        nonlocal close_count
        del identity, api_key
        started.set()
        await continue_creation.wait()

        async def close() -> None:
            nonlocal close_count
            close_count += 1

        return OwnedProviderClient(_FakeClient("retired"), close)

    cache = ProviderClientCache(max_entries=1, factory=factory)
    identity = _identity()
    acquiring = asyncio.create_task(cache.acquire(identity, "secret"))
    await started.wait()

    await cache.retire(identity)
    continue_creation.set()

    with pytest.raises(ProviderClientRetiredError):
        await acquiring
    assert close_count == 1
    assert cache.entry_count == 0
    await cache.aclose()


@pytest.mark.asyncio
async def test_shutdown_rejects_new_leases_and_waits_for_active_lease() -> None:
    factory = _Factory()
    cache = ProviderClientCache(max_entries=1, factory=factory)
    identity = _identity()
    lease = await cache.acquire(identity, "secret")

    shutdown = asyncio.create_task(cache.aclose())
    await asyncio.sleep(0)

    assert not shutdown.done()
    with pytest.raises(ProviderClientCacheClosedError):
        await cache.acquire(identity, "secret")
    assert factory.closed == Counter()

    await lease.release()
    await asyncio.wait_for(shutdown, timeout=1)
    assert cache.closed is True
    assert cache.entry_count == 0
    assert factory.closed == Counter({"client-1": 1})


@pytest.mark.asyncio
async def test_cancellation_cannot_interrupt_lease_release() -> None:
    factory = _Factory()
    cache = ProviderClientCache(max_entries=1, factory=factory)
    lease = await cache.acquire(_identity(), "secret")

    await cache._condition.acquire()  # noqa: SLF001 - force the cleanup race
    releasing = asyncio.create_task(lease.release())
    await asyncio.sleep(0)
    releasing.cancel()
    await asyncio.sleep(0)

    assert not releasing.done()
    cache._condition.release()  # noqa: SLF001
    with pytest.raises(asyncio.CancelledError):
        await releasing

    await asyncio.wait_for(cache.aclose(), timeout=1)
    assert cache.entry_count == 0
    assert factory.closed == Counter({"client-1": 1})


@pytest.mark.asyncio
async def test_cancellation_after_client_creation_closes_reserved_client() -> None:
    allow_return = asyncio.Event()
    factory_returned = asyncio.Event()
    closed = 0

    async def factory(
        identity: ProviderClientIdentity,
        api_key: str | None,
    ) -> OwnedProviderClient:
        nonlocal closed
        del identity, api_key
        await allow_return.wait()

        async def close() -> None:
            nonlocal closed
            closed += 1

        owned = OwnedProviderClient(_FakeClient("created"), close)
        factory_returned.set()
        return owned

    cache = ProviderClientCache(max_entries=1, factory=factory)
    acquiring = asyncio.create_task(cache.acquire(_identity(), "secret"))
    await asyncio.sleep(0)

    await cache._condition.acquire()  # noqa: SLF001 - block cache publication
    allow_return.set()
    await factory_returned.wait()
    await asyncio.sleep(0)
    acquiring.cancel()
    await asyncio.sleep(0)

    cache._condition.release()  # noqa: SLF001
    with pytest.raises(asyncio.CancelledError):
        await acquiring

    assert closed == 1
    assert cache.entry_count == 0
    await cache.aclose()


@pytest.mark.asyncio
async def test_cancellation_cannot_interrupt_eviction_close() -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    closed: list[str] = []
    created = 0

    async def factory(
        identity: ProviderClientIdentity,
        api_key: str | None,
    ) -> OwnedProviderClient:
        nonlocal created
        del identity, api_key
        created += 1
        client = _FakeClient(f"client-{created}")

        async def close() -> None:
            if client.name == "client-1":
                close_started.set()
                await allow_close.wait()
            closed.append(client.name)

        return OwnedProviderClient(client, close)

    cache = ProviderClientCache(max_entries=1, factory=factory)
    first = await cache.acquire(_identity(project_id="projectA"), "secret-a")
    await first.release()

    acquiring = asyncio.create_task(
        cache.acquire(_identity(project_id="projectB"), "secret-b")
    )
    await close_started.wait()
    acquiring.cancel()
    await asyncio.sleep(0)

    assert not acquiring.done()
    allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await acquiring

    assert closed == ["client-1"]
    assert cache.entry_count == 0

    replacement = await cache.acquire(
        _identity(project_id="projectB"), "secret-b"
    )
    await replacement.release()
    await cache.aclose()
    assert closed == ["client-1", "client-2"]


@pytest.mark.asyncio
async def test_shutdown_continues_when_one_closer_fails(caplog) -> None:
    close_events: list[str] = []

    async def factory(
        identity: ProviderClientIdentity,
        api_key: str | None,
    ) -> OwnedProviderClient:
        del api_key

        async def close() -> None:
            close_events.append(identity.project_id)
            if identity.project_id == "projectA":
                raise RuntimeError("close failed")

        return OwnedProviderClient(_FakeClient(identity.project_id), close)

    cache = ProviderClientCache(max_entries=2, factory=factory)
    first = await cache.acquire(_identity(project_id="projectA"), "secret-a")
    second = await cache.acquire(_identity(project_id="projectB"), "secret-b")
    await first.release()
    await second.release()

    await cache.aclose()

    assert close_events == ["projectA", "projectB"]
    assert cache.entry_count == 0
    assert "Failed to close provider client" in caplog.text


@pytest.mark.asyncio
async def test_production_factory_uses_provider_specific_closers(monkeypatch) -> None:
    events: list[str] = []

    class AsyncSdkClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def close(self) -> None:
            events.append("async-close")

    class GoogleAsyncClient:
        async def aclose(self) -> None:
            events.append("google-async-close")

    class GoogleClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.aio = GoogleAsyncClient()

        def close(self) -> None:
            events.append("google-sync-close")

    monkeypatch.setattr(client_cache.openai, "AsyncOpenAI", AsyncSdkClient)
    monkeypatch.setattr(client_cache.anthropic, "AsyncAnthropic", AsyncSdkClient)
    monkeypatch.setattr(client_cache.genai, "Client", GoogleClient)

    for provider in ("openai", "xai", "local", "anthropic", "google"):
        identity = _identity(provider=provider)
        owned = await client_cache.build_provider_client(
            identity,
            None if provider == "local" else "provider-secret",
            request_timeout_seconds=9,
        )
        await owned.close()

    assert events == [
        "async-close",
        "async-close",
        "async-close",
        "async-close",
        "google-async-close",
        "google-sync-close",
    ]
