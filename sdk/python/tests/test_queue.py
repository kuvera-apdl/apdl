"""Event queue batching, flushing, and failure re-queue behavior."""

from __future__ import annotations

from conftest import RecordingTransport

from apdl.config import APDLConfig
from apdl.queue import EventQueue


def config(**kwargs) -> APDLConfig:
    base = dict(api_key="proj_t_x", batch_size=2, max_queue_size=3, enable_flags=False)
    base.update(kwargs)
    return APDLConfig(**base)


def event(i: int) -> dict:
    return {"event": f"e{i}", "type": "track", "anonymous_id": "a"}


def test_flush_sends_in_batches():
    transport = RecordingTransport(ok=True)
    queue = EventQueue(config(), transport)
    for i in range(3):
        queue.enqueue(event(i))
    queue.flush()
    assert queue.pending() == 0
    assert [len(p["events"]) for _u, p in transport.posts] == [2, 1]


def test_failed_send_requeues_events():
    transport = RecordingTransport(ok=False)
    queue = EventQueue(config(), transport)
    queue.enqueue(event(0))
    queue.enqueue(event(1))
    queue.flush()
    # Send failed -> events go back on the queue, not lost.
    assert queue.pending() == 2


def test_max_queue_size_drops_oldest():
    transport = RecordingTransport(ok=True)
    queue = EventQueue(config(max_queue_size=2), transport)
    for i in range(4):
        queue.enqueue(event(i))
    names = {e["event"] for e in queue.snapshot()}
    # Cap is 2 but a batch-size flush may have fired; assert the cap held.
    assert queue.pending() <= 2
    assert "e3" in names


def test_url_built_from_endpoint():
    queue = EventQueue(config(endpoint="https://x.example/"), RecordingTransport())
    assert queue._url == "https://x.example/v1/events"
