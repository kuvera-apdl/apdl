"""Event queue batching, flushing, and failure re-queue behavior."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import RecordingTransport

from apdl.config import APDLConfig
from apdl.queue import EventQueue
from apdl.transport import TransportOutcome
from apdl.types import (
    MAX_REQUEST_SERIALIZED_BYTES,
    MAX_STRING_PROPERTY_LENGTH,
    serialized_json_size,
)


def config(**kwargs) -> APDLConfig:
    base = dict(
        api_key="proj_t_0123456789abcdef",
        endpoint="https://apdl.test",
        batch_size=2,
        max_queue_size=3,
        enable_flags=False,
    )
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
    message_ids = [item["message_id"] for item in queue.snapshot()]
    queue.flush()
    # Send failed -> events go back on the queue, not lost.
    assert queue.pending() == 2
    assert [item["message_id"] for item in queue.snapshot()] == message_ids


class ScriptedTransport(RecordingTransport):
    def __init__(self, outcomes: list[TransportOutcome]) -> None:
        super().__init__()
        self.outcomes = outcomes

    def post_json(self, url: str, payload: Any) -> TransportOutcome:
        self.posts.append((url, payload))
        return self.outcomes.pop(0)


class InvalidOutcomeTransport(RecordingTransport):
    def post_json(self, url: str, payload: Any) -> Any:
        self.posts.append((url, payload))
        return None


def test_permanent_rejection_does_not_block_next_batch():
    transport = ScriptedTransport([
        TransportOutcome.PERMANENT_REJECTION,
        TransportOutcome.ACCEPTED,
    ])
    queue = EventQueue(config(batch_size=1), transport)
    queue.enqueue(event(0))
    queue.enqueue(event(1))
    second_message_id = queue.snapshot()[1]["message_id"]

    queue.flush()

    assert queue.pending() == 0
    assert len(transport.posts) == 2
    assert transport.posts[1][1]["events"][0]["message_id"] == second_message_id


def test_payload_rejection_isolates_invalid_event_and_delivers_neighbors_in_order():
    transport = ScriptedTransport([
        TransportOutcome.PAYLOAD_REJECTED,
        TransportOutcome.ACCEPTED,
        TransportOutcome.PAYLOAD_REJECTED,
        TransportOutcome.PAYLOAD_REJECTED,
        TransportOutcome.ACCEPTED,
    ])
    queue = EventQueue(config(batch_size=3, max_queue_size=3), transport)
    for index in range(3):
        queue.enqueue(event(index))

    report = queue.flush()

    assert report.accepted == 2
    assert report.permanently_rejected == 1
    assert report.undelivered == 0
    assert [
        [item["event"] for item in payload["events"]]
        for _url, payload in transport.posts
    ] == [
        ["e0", "e1", "e2"],
        ["e0"],
        ["e1", "e2"],
        ["e1"],
        ["e2"],
    ]


def test_permanent_request_rejection_does_not_bisect_batch():
    transport = ScriptedTransport([TransportOutcome.PERMANENT_REJECTION])
    queue = EventQueue(config(batch_size=2), transport)
    queue.enqueue(event(0))
    queue.enqueue(event(1))

    report = queue.flush()

    assert len(transport.posts) == 1
    assert report.accepted == 0
    assert report.permanently_rejected == 2
    assert report.undelivered == 0


def test_retryable_bisection_retains_subset_and_unattempted_neighbors():
    transport = ScriptedTransport([
        TransportOutcome.PAYLOAD_REJECTED,
        TransportOutcome.RETRYABLE,
    ])
    queue = EventQueue(config(batch_size=3, max_queue_size=3), transport)
    for index in range(3):
        queue.enqueue(event(index))
    message_ids = [item["message_id"] for item in queue.snapshot()]

    report = queue.flush()

    assert len(transport.posts) == 2
    assert report.accepted == 0
    assert report.permanently_rejected == 0
    assert [item["message_id"] for item in report.undelivered_events] == message_ids
    assert [item["message_id"] for item in queue.snapshot()] == message_ids


def test_retryable_batch_keeps_stable_id_until_accepted():
    transport = ScriptedTransport([
        TransportOutcome.RETRYABLE,
        TransportOutcome.ACCEPTED,
        TransportOutcome.ACCEPTED,
    ])
    queue = EventQueue(config(batch_size=1), transport)
    queue.enqueue(event(0))
    queue.enqueue(event(1))
    first_message_id = queue.snapshot()[0]["message_id"]

    queue.flush()
    assert queue.pending() == 2
    queue.flush()

    assert queue.pending() == 0
    assert transport.posts[0][1]["events"][0]["message_id"] == first_message_id
    assert transport.posts[1][1]["events"][0]["message_id"] == first_message_id


def test_invalid_transport_outcome_is_treated_as_retryable():
    transport = InvalidOutcomeTransport()
    queue = EventQueue(config(batch_size=1), transport)
    queue.enqueue(event(0))
    message_id = queue.snapshot()[0]["message_id"]

    queue.flush()

    assert queue.pending() == 1
    assert queue.snapshot()[0]["message_id"] == message_id


def test_snapshot_mutation_cannot_poison_queued_event():
    transport = RecordingTransport()
    queue = EventQueue(config(), transport)
    original = event(0)
    original["properties"] = {"valid": True}
    queue.enqueue(original)

    snapshot = queue.snapshot()
    snapshot[0]["properties"]["cycle"] = snapshot[0]
    queue.flush()

    assert queue.pending() == 0
    assert transport.all_events()[0]["properties"] == {"valid": True}


def test_batches_stay_within_serialized_request_limit():
    transport = RecordingTransport()
    queue = EventQueue(config(batch_size=20, max_queue_size=20), transport)
    for index in range(10):
        large = event(index)
        large["properties"] = {
            f"part_{part}": "x" * MAX_STRING_PROPERTY_LENGTH for part in range(7)
        }
        queue.enqueue(large)

    queue.flush()

    assert sum(len(payload["events"]) for _url, payload in transport.posts) == 10
    assert len(transport.posts) > 1
    assert all(
        serialized_json_size(payload) <= MAX_REQUEST_SERIALIZED_BYTES
        for _url, payload in transport.posts
    )


def test_max_queue_size_rejects_new_event_without_evicting_accepted_events():
    transport = RecordingTransport(ok=True)
    queue = EventQueue(config(batch_size=100, max_queue_size=2), transport)
    queue.enqueue(event(0))
    queue.enqueue(event(1))

    with pytest.raises(BufferError, match="queue is full"):
        queue.enqueue(event(2))

    assert [item["event"] for item in queue.snapshot()] == ["e0", "e1"]


def test_url_built_from_endpoint():
    queue = EventQueue(config(endpoint="https://x.example/"), RecordingTransport())
    assert queue._url == "https://x.example/v1/events"
