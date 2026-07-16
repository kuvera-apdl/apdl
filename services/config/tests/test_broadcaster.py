"""Bounded SSE broadcaster admission, fan-out, and closure tests."""

import pytest
import pytest_asyncio

from app.sse.broadcaster import (
    ConnectionQuotaExceeded,
    SSEBroadcaster,
    SSESettings,
)


@pytest_asyncio.fixture
async def broadcaster():
    instance = SSEBroadcaster()
    yield instance
    await instance.stop()


async def add(
    broadcaster: SSEBroadcaster,
    project: str = "proj_1",
    credential: str = "credential_1",
    ip: str = "192.0.2.1",
):
    return await broadcaster.add_connection(project, credential, ip)


@pytest.mark.asyncio
async def test_add_and_remove_connection(broadcaster):
    subscription = await add(broadcaster)

    assert await broadcaster.connection_count("proj_1") == 1
    assert subscription.connection_id

    await broadcaster.remove_connection(subscription)
    assert await broadcaster.connection_count("proj_1") == 0


@pytest.mark.asyncio
async def test_connections_are_counted_across_projects(broadcaster):
    first = await add(broadcaster)
    second = await add(
        broadcaster,
        project="proj_2",
        credential="credential_2",
        ip="192.0.2.2",
    )

    assert await broadcaster.connection_count("proj_1") == 1
    assert await broadcaster.connection_count("proj_2") == 1
    assert await broadcaster.total_connection_count() == 2

    await broadcaster.remove_connection(first)
    await broadcaster.remove_connection(second)


@pytest.mark.asyncio
async def test_broadcast_is_project_scoped_and_versioned(broadcaster):
    target = await add(broadcaster)
    other = await add(
        broadcaster,
        project="proj_2",
        credential="credential_2",
        ip="192.0.2.2",
    )

    await broadcaster.broadcast(
        "proj_1",
        "flag_update",
        '{"key":"checkout"}',
        project_version=9,
    )

    queued = target.queue.get_nowait()
    assert queued.project_version == 9
    encoded = queued.event.encode().decode()
    assert "id: 9" in encoded
    assert "event: flag_update" in encoded
    assert 'data: {"key":"checkout"}' in encoded
    assert other.queue.empty()


@pytest.mark.asyncio
async def test_broadcast_fans_out_to_every_project_connection(broadcaster):
    subscriptions = [
        await add(
            broadcaster,
            credential=f"credential_{index}",
            ip=f"192.0.2.{index}",
        )
        for index in range(1, 4)
    ]

    await broadcaster.broadcast("proj_1", "update", "payload", project_version=2)

    assert all(not subscription.queue.empty() for subscription in subscriptions)


@pytest.mark.asyncio
async def test_queue_overflow_signals_close_and_retains_quota_until_exit():
    broadcaster = SSEBroadcaster(SSESettings(queue_capacity=1))
    subscription = await add(broadcaster)

    await broadcaster.broadcast("proj_1", "update", "first", project_version=1)
    await broadcaster.broadcast("proj_1", "update", "second", project_version=2)

    assert subscription.close_event.is_set()
    assert subscription.close_reason == "slow_consumer"
    assert await broadcaster.total_connection_count() == 1
    metrics = await broadcaster.metrics_snapshot()
    assert metrics["queue_overflow_total"] == 1

    await broadcaster.remove_connection(subscription)
    assert await broadcaster.total_connection_count() == 0
    assert (await broadcaster.metrics_snapshot())["closed_total"] == {
        "slow_consumer": 1
    }


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_closes_every_connection(broadcaster):
    await broadcaster.start()
    subscription = await add(broadcaster)

    await broadcaster.stop()
    await broadcaster.stop()

    assert subscription.close_event.is_set()
    assert subscription.close_reason == "server_shutdown"
    assert await broadcaster.total_connection_count() == 0


@pytest.mark.parametrize(
    "settings,expected_scope",
    [
        (
            SSESettings(
                max_connections=1,
                max_connections_per_project=1,
                max_connections_per_credential=1,
                max_connections_per_ip=1,
            ),
            "global",
        ),
        (
            SSESettings(
                max_connections=3,
                max_connections_per_project=1,
                max_connections_per_credential=3,
                max_connections_per_ip=3,
            ),
            "project",
        ),
        (
            SSESettings(
                max_connections=3,
                max_connections_per_project=3,
                max_connections_per_credential=1,
                max_connections_per_ip=3,
            ),
            "credential",
        ),
        (
            SSESettings(
                max_connections=3,
                max_connections_per_project=3,
                max_connections_per_credential=3,
                max_connections_per_ip=1,
            ),
            "ip",
        ),
    ],
)
@pytest.mark.asyncio
async def test_each_admission_quota_fails_atomically(settings, expected_scope):
    broadcaster = SSEBroadcaster(settings)
    first = await add(broadcaster)
    attempted = {
        "global": ("proj_2", "credential_2", "192.0.2.2"),
        "project": ("proj_1", "credential_2", "192.0.2.2"),
        "credential": ("proj_2", "credential_1", "192.0.2.2"),
        "ip": ("proj_2", "credential_2", "192.0.2.1"),
    }[expected_scope]

    with pytest.raises(ConnectionQuotaExceeded) as caught:
        await add(broadcaster, *attempted)

    assert caught.value.scope == expected_scope
    assert await broadcaster.total_connection_count() == 1
    metrics = await broadcaster.metrics_snapshot()
    assert metrics["rejected_total"][expected_scope] == 1

    await broadcaster.remove_connection(first)
    admitted = await add(broadcaster, *attempted)
    assert await broadcaster.total_connection_count() == 1
    await broadcaster.remove_connection(admitted)


@pytest.mark.asyncio
async def test_max_lifetime_signals_observable_close():
    now = [10.0]
    broadcaster = SSEBroadcaster(
        SSESettings(max_lifetime_seconds=5.0),
        clock=lambda: now[0],
    )
    subscription = await add(broadcaster)

    now[0] = 14.9
    await broadcaster.expire_connections()
    assert not subscription.close_event.is_set()

    now[0] = 15.0
    await broadcaster.expire_connections()
    assert subscription.close_event.is_set()
    assert subscription.close_reason == "max_lifetime"
    await broadcaster.remove_connection(subscription)


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"queue_capacity": 0}, "queue_capacity"),
        ({"max_connections": 0}, "max_connections"),
        ({"ping_interval_seconds": 0}, "ping_interval_seconds"),
        ({"ping_interval_seconds": float("nan")}, "ping_interval_seconds"),
        ({"send_timeout_seconds": float("inf")}, "send_timeout_seconds"),
        (
            {"max_connections": 2, "max_connections_per_project": 3},
            "max_connections_per_project",
        ),
    ],
)
def test_settings_reject_unbounded_or_inconsistent_values(kwargs, match):
    with pytest.raises(ValueError, match=match):
        SSESettings(**kwargs)


@pytest.mark.asyncio
async def test_connection_ids_are_unique(broadcaster):
    first = await add(broadcaster)
    second = await add(
        broadcaster,
        credential="credential_2",
        ip="192.0.2.2",
    )
    assert first.connection_id != second.connection_id
