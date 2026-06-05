"""Port of all 16 test cases from C++ test_broadcaster.cpp.

Tests the SSE broadcaster: connection management, broadcast delivery,
dead-connection cleanup, lifecycle, message format, and unique IDs.
"""

import asyncio

import pytest
import pytest_asyncio

from app.sse import broadcaster as broadcaster_module
from app.sse.broadcaster import SSEBroadcaster


@pytest_asyncio.fixture
async def broadcaster():
    b = SSEBroadcaster()
    yield b
    await b.stop()


# ---- Connection management ----


@pytest.mark.asyncio
async def test_add_and_remove_connection(broadcaster):
    queue = asyncio.Queue()
    conn_id = await broadcaster.add_connection("proj_1", queue)

    assert await broadcaster.connection_count("proj_1") == 1
    assert conn_id  # not empty

    await broadcaster.remove_connection("proj_1", conn_id)
    assert await broadcaster.connection_count("proj_1") == 0


@pytest.mark.asyncio
async def test_multiple_connections_same_project(broadcaster):
    _id1 = await broadcaster.add_connection("proj_1", asyncio.Queue())
    id2 = await broadcaster.add_connection("proj_1", asyncio.Queue())
    _id3 = await broadcaster.add_connection("proj_1", asyncio.Queue())

    assert await broadcaster.connection_count("proj_1") == 3
    assert await broadcaster.total_connection_count() == 3

    await broadcaster.remove_connection("proj_1", id2)
    assert await broadcaster.connection_count("proj_1") == 2


@pytest.mark.asyncio
async def test_connections_across_projects(broadcaster):
    await broadcaster.add_connection("proj_1", asyncio.Queue())
    await broadcaster.add_connection("proj_2", asyncio.Queue())
    await broadcaster.add_connection("proj_3", asyncio.Queue())

    assert await broadcaster.connection_count("proj_1") == 1
    assert await broadcaster.connection_count("proj_2") == 1
    assert await broadcaster.connection_count("proj_3") == 1
    assert await broadcaster.total_connection_count() == 3


@pytest.mark.asyncio
async def test_nonexistent_project_has_zero_connections(broadcaster):
    assert await broadcaster.connection_count("nonexistent") == 0


# ---- Broadcasting ----


@pytest.mark.asyncio
async def test_broadcast_to_connections(broadcaster):
    queue = asyncio.Queue()
    await broadcaster.add_connection("proj_1", queue)

    await broadcaster.broadcast(
        "proj_1", "flag_update", '{"key":"test","enabled":true}'
    )

    assert not queue.empty()
    msg = queue.get_nowait()
    assert "event: flag_update" in msg
    assert "data: " in msg
    assert "id: " in msg


@pytest.mark.asyncio
async def test_broadcast_only_to_target_project(broadcaster):
    queue1 = asyncio.Queue()
    queue2 = asyncio.Queue()

    await broadcaster.add_connection("proj_1", queue1)
    await broadcaster.add_connection("proj_2", queue2)

    await broadcaster.broadcast("proj_1", "test_event", "data")

    assert not queue1.empty()
    assert queue2.empty()


@pytest.mark.asyncio
async def test_broadcast_fanout_to_multiple_connections(broadcaster):
    queues = [asyncio.Queue() for _ in range(3)]
    for q in queues:
        await broadcaster.add_connection("proj_1", q)

    await broadcaster.broadcast("proj_1", "update", "payload")

    for q in queues:
        assert not q.empty()


@pytest.mark.asyncio
async def test_broadcast_to_nonexistent_project_is_noop(broadcaster):
    # Should not crash
    await broadcaster.broadcast("nonexistent", "event", "data")
    assert await broadcaster.total_connection_count() == 0


@pytest.mark.asyncio
async def test_dead_connections_removed_on_broadcast(broadcaster):
    """Dead connections (full queues) are cleaned up during broadcast."""
    # Create a queue with maxsize=0 that is always "full"
    # We simulate a dead connection by using a queue that will raise on put_nowait
    dead_queue = asyncio.Queue(maxsize=1)
    # Fill it so put_nowait raises QueueFull
    dead_queue.put_nowait("filler")

    good_queue = asyncio.Queue()

    await broadcaster.add_connection("proj_1", dead_queue)
    await broadcaster.add_connection("proj_1", good_queue)

    assert await broadcaster.connection_count("proj_1") == 2

    await broadcaster.broadcast("proj_1", "test", "data")

    # Dead connection should be removed
    assert await broadcaster.connection_count("proj_1") == 1
    assert not good_queue.empty()


# ---- Remove edge cases ----


@pytest.mark.asyncio
async def test_remove_nonexistent_connection_is_noop(broadcaster):
    await broadcaster.add_connection("proj_1", asyncio.Queue())
    assert await broadcaster.connection_count("proj_1") == 1

    # Removing nonexistent connection should not affect existing ones
    await broadcaster.remove_connection("proj_1", "does_not_exist")
    assert await broadcaster.connection_count("proj_1") == 1


@pytest.mark.asyncio
async def test_remove_from_nonexistent_project_is_noop(broadcaster):
    await broadcaster.remove_connection("nonexistent", "some_id")
    assert await broadcaster.total_connection_count() == 0


# ---- Lifecycle ----


@pytest.mark.asyncio
async def test_start_stop_lifecycle(broadcaster):
    await broadcaster.start()
    assert await broadcaster.total_connection_count() == 0

    await broadcaster.add_connection("proj_1", asyncio.Queue())
    assert await broadcaster.total_connection_count() == 1

    await broadcaster.stop()
    assert await broadcaster.total_connection_count() == 0


@pytest.mark.asyncio
async def test_double_start_is_idempotent(broadcaster):
    await broadcaster.start()
    await broadcaster.start()  # Should not crash or create duplicate tasks
    await broadcaster.stop()


@pytest.mark.asyncio
async def test_double_stop_is_idempotent(broadcaster):
    await broadcaster.start()
    await broadcaster.stop()
    await broadcaster.stop()  # Should not crash


@pytest.mark.asyncio
async def test_heartbeat_is_typed_sse_event(monkeypatch):
    monkeypatch.setattr(broadcaster_module, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    b = SSEBroadcaster()
    queue = asyncio.Queue()
    await b.add_connection("proj_1", queue)

    try:
        await b.start()
        msg = await asyncio.wait_for(queue.get(), timeout=0.2)
    finally:
        await b.stop()

    assert msg == "event: heartbeat\ndata: {}\n\n"


# ---- SSE message format ----


@pytest.mark.asyncio
async def test_sse_message_format(broadcaster):
    queue = asyncio.Queue()
    await broadcaster.add_connection("proj_1", queue)

    await broadcaster.broadcast(
        "proj_1", "config_change", '{"hello":"world"}'
    )

    msg = queue.get_nowait()

    # Verify SSE format: id, event, data lines, followed by empty line
    assert "id: " in msg
    assert "event: config_change\n" in msg
    assert 'data: {"hello":"world"}\n' in msg
    # Should end with \n\n
    assert msg.endswith("\n\n")


# ---- Unique IDs ----


@pytest.mark.asyncio
async def test_unique_connection_ids(broadcaster):
    id1 = await broadcaster.add_connection("proj_1", asyncio.Queue())
    id2 = await broadcaster.add_connection("proj_1", asyncio.Queue())
    id3 = await broadcaster.add_connection("proj_2", asyncio.Queue())

    assert id1 != id2
    assert id2 != id3
    assert id1 != id3
