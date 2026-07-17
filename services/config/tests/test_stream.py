"""SSE route interleaving, slow-consumer, and admission integration tests."""

import asyncio
from contextlib import asynccontextmanager

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.auth import Principal
from app.main import app
from app.routers import stream
from app.sse.broadcaster import SSEBroadcaster, SSESettings


class CredentialConnection:
    def __init__(self, results: bool | list[object] = True) -> None:
        self.results = list(results) if isinstance(results, list) else [results]
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchval(self, query: str, *args):
        self.calls.append((query, args))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class CredentialPool:
    def __init__(self, results: bool | list[object] = True) -> None:
        self.connection = CredentialConnection(results)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def make_request() -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/stream",
            "raw_path": b"/v1/stream",
            "query_string": b"",
            "headers": [],
            "client": ("192.0.2.10", 12345),
            "server": ("test", 80),
            "app": app,
        }
    )
    request.state.principal = Principal(
        credential_id="credential-1",
        project_id="apdl",
        roles=frozenset({"config:read"}),
    )
    return request


def make_flag(*, version: int, description: str = "") -> dict:
    return {
        "key": "checkout",
        "project_id": "apdl",
        "name": "Checkout",
        "state": "active",
        "owners": [],
        "review_by": None,
        "description": description,
        "enabled": True,
        "default_variant": "control",
        "variants": [{"key": "control", "weight": 1}],
        "rules": [],
        "fallthrough": {
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"}
        },
        "salt": "salt",
        "evaluation_mode": "client",
        "auto_disable": False,
        "guardrails": [],
        "disabled_reason": "",
        "disabled_by": "",
        "disabled_at": None,
        "version": version,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "archived_at": None,
    }


def encoded(event) -> str:
    return event.encode().decode()


@pytest.fixture
def route_state():
    original = dict(app.state._state)
    app.state.pg_pool = CredentialPool()
    app.state.trusted_proxy_networks = ()
    yield
    app.state._state.clear()
    app.state._state.update(original)


@pytest.mark.asyncio
async def test_subscription_precedes_snapshot_and_preserves_exact_interleaving(
    monkeypatch,
    route_state,
):
    broadcaster = SSEBroadcaster()
    app.state.broadcaster = broadcaster

    async def snapshot(pool, project_id, *, client_visible_only):
        assert pool is app.state.pg_pool
        assert project_id == "apdl"
        assert client_visible_only is True
        assert await broadcaster.connection_count("apdl") == 1
        await broadcaster.broadcast(
            "apdl",
            "flag_update",
            '{"version":2}',
            project_version=2,
        )
        return [make_flag(version=1)], 1

    monkeypatch.setattr(stream.pg_store, "get_flag_snapshot", snapshot)

    response = await stream.sse_stream(make_request())
    iterator = response.body_iterator
    initial = await anext(iterator)
    update = await anext(iterator)

    assert "event: config" in encoded(initial)
    assert "id: 1" in encoded(initial)
    assert "event: flag_update" in encoded(update)
    assert "id: 2" in encoded(update)
    await iterator.aclose()
    assert await broadcaster.total_connection_count() == 0


@pytest.mark.asyncio
async def test_snapshot_version_suppresses_already_included_queue_event(
    monkeypatch,
    route_state,
):
    broadcaster = SSEBroadcaster()
    app.state.broadcaster = broadcaster

    async def snapshot(*args, **kwargs):
        await broadcaster.broadcast(
            "apdl",
            "flag_update",
            '{"version":2}',
            project_version=2,
        )
        return [make_flag(version=2)], 2

    monkeypatch.setattr(stream.pg_store, "get_flag_snapshot", snapshot)
    response = await stream.sse_stream(make_request())
    iterator = response.body_iterator
    assert "id: 2" in encoded(await anext(iterator))

    await broadcaster.broadcast(
        "apdl",
        "flag_update",
        '{"version":3}',
        project_version=3,
    )
    next_event = await anext(iterator)
    assert "id: 3" in encoded(next_event)
    assert 'data: {"version":3}' in encoded(next_event)
    await iterator.aclose()


@pytest.mark.asyncio
async def test_stream_closes_when_credential_loses_config_read(
    monkeypatch,
    route_state,
):
    broadcaster = SSEBroadcaster(SSESettings(credential_check_interval_seconds=0.01))
    app.state.broadcaster = broadcaster
    app.state.pg_pool = CredentialPool([True, False])
    monkeypatch.setattr(
        stream.pg_store,
        "get_flag_snapshot",
        AsyncMock(return_value=([make_flag(version=1)], 1)),
    )

    response = await stream.sse_stream(make_request())
    iterator = response.body_iterator
    assert "event: config" in encoded(await anext(iterator))

    terminal = encoded(await asyncio.wait_for(anext(iterator), timeout=0.2))
    await iterator.aclose()

    assert "event: stream_error" in terminal
    assert '"reason":"credential_revoked"' in terminal
    query, args = app.state.pg_pool.connection.calls[-1]
    assert "FROM auth_credentials" in query
    assert "$3::TEXT = ANY(roles)" in query
    assert args == ("credential-1", "apdl", "config:read")
    assert (await broadcaster.metrics_snapshot())["closed_total"] == {
        "credential_revoked": 1
    }


@pytest.mark.asyncio
async def test_stream_fails_closed_when_credential_registry_becomes_unavailable(
    monkeypatch,
    route_state,
):
    broadcaster = SSEBroadcaster(SSESettings(credential_check_interval_seconds=0.01))
    app.state.broadcaster = broadcaster
    app.state.pg_pool = CredentialPool([True, ConnectionError("postgres down")])
    monkeypatch.setattr(
        stream.pg_store,
        "get_flag_snapshot",
        AsyncMock(return_value=([make_flag(version=1)], 1)),
    )

    response = await stream.sse_stream(make_request())
    iterator = response.body_iterator
    assert "event: config" in encoded(await anext(iterator))

    terminal = encoded(await asyncio.wait_for(anext(iterator), timeout=0.2))
    await iterator.aclose()

    assert "event: stream_error" in terminal
    assert '"reason":"credential_authority_unavailable"' in terminal
    assert (await broadcaster.metrics_snapshot())["closed_total"] == {
        "credential_authority_unavailable": 1
    }


@pytest.mark.asyncio
async def test_stream_rechecks_credential_before_emitting_the_snapshot(
    monkeypatch,
    route_state,
):
    broadcaster = SSEBroadcaster()
    app.state.broadcaster = broadcaster
    app.state.pg_pool = CredentialPool(False)
    monkeypatch.setattr(
        stream.pg_store,
        "get_flag_snapshot",
        AsyncMock(return_value=([make_flag(version=1)], 1)),
    )

    response = await stream.sse_stream(make_request())
    iterator = response.body_iterator
    first = encoded(await anext(iterator))
    await iterator.aclose()

    assert "event: config" not in first
    assert '"reason":"credential_revoked"' in first


@pytest.mark.asyncio
async def test_slow_consumer_observes_close_then_reconnects_to_full_snapshot(
    monkeypatch,
    route_state,
):
    broadcaster = SSEBroadcaster(SSESettings(queue_capacity=1))
    app.state.broadcaster = broadcaster
    snapshots = AsyncMock(
        side_effect=[
            ([make_flag(version=1)], 1),
            ([make_flag(version=3, description="latest")], 3),
        ]
    )
    monkeypatch.setattr(stream.pg_store, "get_flag_snapshot", snapshots)

    request = make_request()
    response = await stream.sse_stream(request)
    first_body_started = asyncio.Event()
    release_transport = asyncio.Event()
    sent_messages: list[dict] = []

    async def receive():
        await asyncio.Event().wait()

    async def slow_send(message):
        if (
            message["type"] == "http.response.body"
            and message.get("body")
            and not first_body_started.is_set()
        ):
            first_body_started.set()
            await release_transport.wait()
        sent_messages.append(message)

    response_task = asyncio.create_task(response(request.scope, receive, slow_send))
    await asyncio.wait_for(first_body_started.wait(), timeout=1)

    await broadcaster.broadcast(
        "apdl",
        "flag_update",
        '{"version":2}',
        project_version=2,
    )
    await broadcaster.broadcast(
        "apdl",
        "flag_update",
        '{"version":3}',
        project_version=3,
    )

    release_transport.set()
    await asyncio.wait_for(response_task, timeout=1)
    response_body = b"".join(
        message.get("body", b"")
        for message in sent_messages
        if message["type"] == "http.response.body"
    ).decode()

    assert "event: config" in response_body
    assert "event: stream_error" in response_body
    assert '"reason":"slow_consumer"' in response_body
    assert '"snapshot_required":true' in response_body
    assert sent_messages[-1] == {
        "type": "http.response.body",
        "body": b"",
        "more_body": False,
    }
    assert await broadcaster.total_connection_count() == 0

    reconnected = await stream.sse_stream(make_request())
    reconnect_iterator = reconnected.body_iterator
    snapshot_text = encoded(await anext(reconnect_iterator))
    assert "id: 3" in snapshot_text
    assert '"version":3' in snapshot_text
    await reconnect_iterator.aclose()


@pytest.mark.asyncio
async def test_snapshot_failure_releases_admission_capacity(monkeypatch, route_state):
    broadcaster = SSEBroadcaster()
    app.state.broadcaster = broadcaster
    monkeypatch.setattr(
        stream.pg_store,
        "get_flag_snapshot",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await stream.sse_stream(make_request())

    assert await broadcaster.total_connection_count() == 0


@pytest.mark.asyncio
async def test_quota_failure_is_bounded_before_stream_headers(monkeypatch, route_state):
    broadcaster = SSEBroadcaster(
        SSESettings(
            max_connections=1,
            max_connections_per_project=1,
            max_connections_per_credential=1,
            max_connections_per_ip=1,
        )
    )
    app.state.broadcaster = broadcaster
    existing = await broadcaster.add_connection(
        "other",
        "other-credential",
        "192.0.2.20",
    )
    snapshot = AsyncMock()
    monkeypatch.setattr(stream.pg_store, "get_flag_snapshot", snapshot)

    with pytest.raises(HTTPException) as caught:
        await stream.sse_stream(make_request())

    assert caught.value.status_code == 429
    assert caught.value.detail == {
        "error": "sse_connection_limit",
        "scope": "global",
    }
    assert caught.value.headers == {"Retry-After": "5"}
    snapshot.assert_not_awaited()
    await broadcaster.remove_connection(existing)
