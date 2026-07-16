"""Deterministic flush/shutdown ownership and undelivered-event reporting."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from conftest import RecordingTransport

from apdl import APDLClient, APDLConfig, DeliveryReport
from apdl.queue import EventQueue
from apdl.transport import TransportOutcome


def config(**kwargs: Any) -> APDLConfig:
    values = {
        "api_key": "proj_test_0123456789abcdef",
        "endpoint": "https://apdl.test",
        "enable_flags": False,
        "batch_size": 100,
        "max_queue_size": 100,
        "flush_interval": 60.0,
    }
    values.update(kwargs)
    return APDLConfig(**values)


def make_client(transport: RecordingTransport, **kwargs: Any) -> APDLClient:
    return APDLClient(config(**kwargs), transport=transport)


def event(index: int) -> dict[str, Any]:
    return {"event": f"event_{index}", "type": "track", "anonymous_id": "anon_1"}


class BlockingTransport(RecordingTransport):
    def __init__(self, outcome: TransportOutcome) -> None:
        super().__init__(outcome=outcome)
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel_called = threading.Event()
        self._active_lock = threading.Lock()
        self._active = False
        self.closed_while_active = False

    def post_json(self, url: str, payload: Any) -> TransportOutcome:
        self.posts.append((url, payload))
        with self._active_lock:
            self._active = True
        self.started.set()
        try:
            if not self.release.wait(timeout=5):
                raise RuntimeError("test transport was not released")
            return self.outcome
        finally:
            with self._active_lock:
                self._active = False

    def cancel_retries(self) -> None:
        super().cancel_retries()
        self.cancel_called.set()

    def close(self) -> None:
        with self._active_lock:
            self.closed_while_active = self._active
        super().close()


class ConcurrentTransport(RecordingTransport):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self._active_lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def post_json(self, url: str, payload: Any) -> TransportOutcome:
        self.posts.append((url, payload))
        with self._active_lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        self.started.set()
        try:
            if not self.release.wait(timeout=5):
                raise RuntimeError("test transport was not released")
            return TransportOutcome.ACCEPTED
        finally:
            with self._active_lock:
                self._active -= 1


def test_shutdown_drains_all_events_before_closing_transport():
    transport = RecordingTransport()
    client = make_client(transport)
    for index in range(3):
        client.track(f"event_{index}", anonymous_id="anon_1")

    report = client.shutdown()

    assert isinstance(report, DeliveryReport)
    assert report.accepted == 3
    assert report.permanently_rejected == 0
    assert report.undelivered == 0
    assert report.complete is True
    assert client.pending_events == 0
    assert transport.closed is True
    assert transport.close_calls == 1


def test_shutdown_retains_and_returns_retryable_events_with_stable_ids():
    transport = RecordingTransport(outcome=TransportOutcome.RETRYABLE)
    client = make_client(transport)
    client.track(
        "checkout",
        {"nested": {"step": 1}},
        anonymous_id="anon_1",
    )

    first = client.shutdown()
    posted_id = transport.posts[0][1]["events"][0]["message_id"]

    assert first.accepted == 0
    assert first.permanently_rejected == 0
    assert first.undelivered == 1
    assert first.complete is False
    assert first.undelivered_events[0]["message_id"] == posted_id
    assert client.pending_events == 1
    assert transport.retries_cancelled is True

    # Reports returned to callers are detached from the retained queue and the
    # client's idempotent stored shutdown result.
    first.undelivered_events[0]["properties"]["nested"]["step"] = 99
    second = client.shutdown()
    after_close = client.flush()

    assert second.undelivered_events[0]["properties"]["nested"]["step"] == 1
    assert after_close.undelivered_events[0]["properties"]["nested"]["step"] == 1
    assert len(transport.posts) == 1
    assert transport.close_calls == 1


def test_shutdown_reports_permanent_rejections_without_retaining_them():
    transport = RecordingTransport(outcome=TransportOutcome.PERMANENT_REJECTION)
    client = make_client(transport)
    client.track("one", anonymous_id="anon_1")
    client.track("two", anonymous_id="anon_1")

    report = client.shutdown()

    assert report.accepted == 0
    assert report.permanently_rejected == 2
    assert report.undelivered == 0
    assert client.pending_events == 0


def test_shutdown_waits_for_active_request_and_concurrent_callers_share_result():
    transport = BlockingTransport(TransportOutcome.RETRYABLE)
    client = make_client(transport, batch_size=1)
    client.track("checkout", anonymous_id="anon_1")
    assert transport.started.wait(timeout=1)
    posted_id = transport.posts[0][1]["events"][0]["message_id"]

    reports: list[DeliveryReport] = []
    errors: list[BaseException] = []

    def shut_down() -> None:
        try:
            reports.append(client.shutdown())
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=shut_down)
    first.start()
    with client._lifecycle:
        assert client._lifecycle.wait_for(lambda: client._closing, timeout=1)
    assert transport.cancel_called.wait(timeout=1)

    second = threading.Thread(target=shut_down)
    second.start()

    assert first.is_alive()
    assert second.is_alive()
    assert transport.closed is False
    with pytest.raises(RuntimeError, match="shutting down"):
        client.track("late", anonymous_id="anon_1")

    transport.release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(reports) == 2
    assert reports[0] == reports[1]
    assert reports[0].undelivered_events[0]["message_id"] == posted_id
    assert transport.closed_while_active is False
    assert transport.close_calls == 1


def test_shutdown_does_not_repeat_an_active_accepted_drain():
    transport = BlockingTransport(TransportOutcome.ACCEPTED)
    client = make_client(transport, batch_size=1)
    client.track("checkout", anonymous_id="anon_1")
    assert transport.started.wait(timeout=1)

    reports: list[DeliveryReport] = []
    worker = threading.Thread(target=lambda: reports.append(client.shutdown()))
    worker.start()
    assert transport.cancel_called.wait(timeout=1)
    assert transport.closed is False

    transport.release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(transport.posts) == 1
    assert reports[0].accepted == 1
    assert reports[0].undelivered == 0
    assert transport.closed_while_active is False


def test_tracking_methods_reject_after_shutdown():
    client = make_client(RecordingTransport())
    client.shutdown()

    calls = [
        lambda: client.track("late", anonymous_id="anon_1"),
        lambda: client.identify("user_1"),
        lambda: client.group("group_1", user_id="user_1"),
        lambda: client.page("/late", anonymous_id="anon_1"),
    ]
    for call in calls:
        with pytest.raises(RuntimeError, match="shutting down"):
            call()


def test_queue_rejects_enqueue_and_restart_after_stop():
    transport = RecordingTransport()
    queue = EventQueue(config(), transport)
    queue.enqueue(event(0))

    report = queue.stop()

    assert report.accepted == 1
    with pytest.raises(RuntimeError, match="shutting down"):
        queue.enqueue(event(1))
    with pytest.raises(RuntimeError, match="cannot restart"):
        queue.start()


def test_concurrent_flushes_are_serialized_without_duplicate_delivery():
    transport = ConcurrentTransport()
    queue = EventQueue(config(batch_size=1), transport)
    queue.enqueue(event(0))
    queue.enqueue(event(1))

    reports: list[DeliveryReport] = []
    second_invoked = threading.Event()

    first = threading.Thread(target=lambda: reports.append(queue.flush()))

    def second_flush() -> None:
        second_invoked.set()
        reports.append(queue.flush())

    second = threading.Thread(target=second_flush)
    first.start()
    assert transport.started.wait(timeout=1)
    second.start()
    assert second_invoked.wait(timeout=1)

    with transport._active_lock:
        assert transport._active == 1
    transport.release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert transport.max_active == 1
    assert sum(report.accepted for report in reports) == 2
    delivered = [
        item["message_id"]
        for _url, payload in transport.posts
        for item in payload["events"]
    ]
    assert len(delivered) == len(set(delivered)) == 2


def test_retry_requeue_preserves_inflight_and_concurrent_intake_over_capacity():
    transport = BlockingTransport(TransportOutcome.RETRYABLE)
    queue = EventQueue(config(batch_size=1, max_queue_size=2), transport)
    queue.enqueue(event(0))
    queue.enqueue(event(1))

    reports: list[DeliveryReport] = []
    worker = threading.Thread(target=lambda: reports.append(queue.flush()))
    worker.start()
    assert transport.started.wait(timeout=1)

    # The in-flight event freed one intake slot. Filling it must not make the
    # original event evictable if the request later needs retry.
    queue.enqueue(event(2))
    transport.release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert reports[0].undelivered == 3
    assert [item["event"] for item in queue.snapshot()] == [
        "event_0",
        "event_1",
        "event_2",
    ]
    with pytest.raises(BufferError, match="queue is full"):
        queue.enqueue(event(3))


def test_client_reports_queue_capacity_rejection_synchronously():
    transport = RecordingTransport()
    client = make_client(transport, max_queue_size=2)
    client.track("one", anonymous_id="anon_1")
    client.track("two", anonymous_id="anon_1")

    with pytest.raises(BufferError, match="queue is full"):
        client.track("three", anonymous_id="anon_1")

    report = client.shutdown()
    assert report.accepted == 2
    assert {item["event"] for item in transport.all_events()} == {"one", "two"}
