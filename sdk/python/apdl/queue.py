"""Batching event queue with a background flush thread.

Server-side analogue of ``sdk/javascript/src/core/event-queue.ts``. Canonical
JSON events are buffered and sent in count- and byte-bounded batches either
when ``batch_size`` is reached or every ``flush_interval`` seconds by a daemon
thread. Retryable batches are re-queued at the front without changing IDs; a
permanent rejection is discarded so it cannot block valid neighboring events.
Once ``max_queue_size`` is reached, new intake is rejected synchronously rather
than evicting an event the SDK already accepted.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .config import APDLConfig
from .transport import Transport, TransportOutcome
from .types import (
    MAX_REQUEST_SERIALIZED_BYTES,
    canonicalize_event_payload,
    serialized_json_size,
)

logger = logging.getLogger("apdl")


@dataclass(frozen=True)
class DeliveryReport:
    """Outcome of one flush or shutdown drain.

    Retryable events remain owned by the closed queue and are returned as a
    detached snapshot so the host can persist or replay them explicitly.
    """

    accepted: int
    permanently_rejected: int
    undelivered_events: tuple[dict[str, Any], ...]

    @property
    def undelivered(self) -> int:
        return len(self.undelivered_events)

    @property
    def complete(self) -> bool:
        return not self.undelivered_events


class EventQueue:
    def __init__(self, config: APDLConfig, transport: Transport) -> None:
        self._config = config
        self._transport = transport
        self._url = f"{config.endpoint}/v1/events"

        self._queue: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._drain_lock = threading.Lock()
        self._drain_state_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._accepting = True
        self._closed = False
        self._drain_active = False
        self._last_drain_report = DeliveryReport(0, 0, ())
        self._shutdown_report: DeliveryReport | None = None

    def enqueue(self, event: dict[str, Any]) -> None:
        with self._lock:
            if not self._accepting:
                raise RuntimeError("APDL: event queue is shutting down")
            if len(self._queue) >= self._config.max_queue_size:
                raise BufferError("APDL: event queue is full; new event rejected")
        canonical = canonicalize_event_payload(event)
        with self._lock:
            if not self._accepting:
                raise RuntimeError("APDL: event queue is shutting down")
            if len(self._queue) >= self._config.max_queue_size:
                raise BufferError("APDL: event queue is full; new event rejected")
            self._queue.append(canonical)
            should_flush = len(self._queue) >= self._config.batch_size
        if should_flush:
            self._wake.set()

    def start(self) -> None:
        with self._lock:
            if not self._accepting:
                raise RuntimeError("APDL: event queue cannot restart after shutdown")
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run, name="apdl-flush", daemon=True
            )
            self._thread.start()

    def stop(self) -> DeliveryReport:
        """Fence intake, finish one drain attempt, and retain retryable events.

        Retry sleeps are cancelled immediately. An in-flight HTTP request is
        allowed to finish within its configured timeout; the transport remains
        open until this method returns to its owner.
        """
        with self._stop_lock:
            with self._lock:
                if self._closed:
                    assert self._shutdown_report is not None
                    return _copy_report(self._shutdown_report)
                self._accepting = False

            self._stopping.set()
            self._wake.set()
            cancel_retries = getattr(self._transport, "cancel_retries", None)
            if callable(cancel_retries):
                cancel_retries()

            with self._drain_state_lock:
                had_active_drain = self._drain_active

            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join()

            with self._drain_lock:
                if had_active_drain:
                    with self._drain_state_lock:
                        attempt = self._last_drain_report
                    report = self._report(
                        accepted=attempt.accepted,
                        permanently_rejected=attempt.permanently_rejected,
                    )
                else:
                    report = self._drain_locked()

            with self._lock:
                self._closed = True
                self._thread = None
                self._shutdown_report = report
            return _copy_report(report)

    def flush(self) -> DeliveryReport:
        """Synchronously drains the queue, sending every pending batch.

        Stops early if a batch has a retryable result: the failed batch is
        re-queued at the front, so retrying it in the same loop would spin
        forever. Permanently rejected batches are discarded and draining
        continues so valid events behind them still make progress.
        """
        with self._drain_lock:
            with self._drain_state_lock:
                can_drain = not self._stopping.is_set() and not self._closed
                if can_drain:
                    self._drain_active = True
            if not can_drain:
                return self._report()

            report: DeliveryReport | None = None
            try:
                report = self._drain_locked()
                return report
            finally:
                with self._drain_state_lock:
                    if report is not None:
                        self._last_drain_report = report
                    self._drain_active = False

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            # Do not expose mutable references to queue-owned records. A caller
            # mutating a diagnostic snapshot must not be able to introduce a
            # serialization poison after pre-enqueue validation.
            return deepcopy(list(self._queue))

    # ── Internals ─────────────────────────────────────────────────

    def _run(self) -> None:
        while True:
            self._wake.wait(timeout=self._config.flush_interval)
            self._wake.clear()
            if self._stopping.is_set():
                return
            self.flush()

    def _drain_locked(self) -> DeliveryReport:
        accepted = 0
        permanently_rejected = 0
        while True:
            batch = self._take_batch()
            if not batch:
                return self._report(accepted, permanently_rejected)
            outcome = self._send(batch)
            if outcome is TransportOutcome.ACCEPTED:
                accepted += len(batch)
                continue
            if outcome is TransportOutcome.PERMANENT_REJECTION:
                permanently_rejected += len(batch)
                continue
            return self._report(accepted, permanently_rejected)

    def _take_batch(self) -> list[dict[str, Any]]:
        with self._lock:
            batch: list[dict[str, Any]] = []
            for _ in range(self._config.batch_size):
                if not self._queue:
                    break
                event = self._queue[0]
                candidate = [*batch, event]
                if (
                    batch
                    and serialized_json_size({"events": candidate})
                    > MAX_REQUEST_SERIALIZED_BYTES
                ):
                    break
                batch.append(self._queue.popleft())
            return batch

    def _send(self, batch: list[dict[str, Any]]) -> TransportOutcome:
        """Send a batch, re-queuing only a retryable transport outcome."""
        try:
            outcome = self._transport.post_json(self._url, {"events": batch})
            if not isinstance(outcome, TransportOutcome):
                logger.error("APDL: transport returned an invalid outcome")
                outcome = TransportOutcome.RETRYABLE
        except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
            # Pre-queue validation should make this unreachable, but treating
            # encoding errors as permanent prevents one corrupt record from
            # blocking every valid event behind it.
            logger.exception("APDL: permanently rejecting non-serializable batch")
            outcome = TransportOutcome.PERMANENT_REJECTION
        except Exception:  # noqa: BLE001 - never let the flush thread die
            logger.exception("APDL: unexpected error during flush")
            outcome = TransportOutcome.RETRYABLE

        if outcome is TransportOutcome.RETRYABLE:
            if self._config.debug:
                logger.warning("APDL: batch send failed, re-queuing %d events", len(batch))
            self._requeue_front(batch)
            return outcome
        if outcome is TransportOutcome.PERMANENT_REJECTION:
            logger.warning(
                "APDL: permanently rejected %d events; batch will not be retried",
                len(batch),
            )
        return outcome

    def _requeue_front(self, batch: list[dict[str, Any]]) -> None:
        with self._lock:
            self._queue.extendleft(reversed(batch))
            # Concurrent intake can consume the space freed by an in-flight
            # batch. Preserve both sets on retry even if the queue temporarily
            # exceeds its intake cap; later enqueue calls reject until drained.

    def _report(
        self,
        accepted: int = 0,
        permanently_rejected: int = 0,
    ) -> DeliveryReport:
        with self._lock:
            undelivered = tuple(deepcopy(list(self._queue)))
        return DeliveryReport(accepted, permanently_rejected, undelivered)


def _copy_report(report: DeliveryReport) -> DeliveryReport:
    return DeliveryReport(
        report.accepted,
        report.permanently_rejected,
        tuple(deepcopy(list(report.undelivered_events))),
    )
