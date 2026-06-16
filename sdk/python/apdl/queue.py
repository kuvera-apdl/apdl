"""Batching event queue with a background flush thread.

Server-side analogue of ``sdk/javascript/src/core/event-queue.ts``. Events are
buffered and sent in batches either when ``batch_size`` is reached or every
``flush_interval`` seconds by a daemon thread. A failed batch is re-queued at the
front (best-effort) instead of persisted to disk; the oldest events are dropped
once ``max_queue_size`` is exceeded.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

from .config import APDLConfig
from .transport import Transport

logger = logging.getLogger("apdl")


class EventQueue:
    def __init__(self, config: APDLConfig, transport: Transport) -> None:
        self._config = config
        self._transport = transport
        self._url = f"{config.endpoint}/v1/events"

        self._queue: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def enqueue(self, event: dict[str, Any]) -> None:
        with self._lock:
            if len(self._queue) >= self._config.max_queue_size:
                if self._config.debug:
                    logger.warning("APDL: queue full, dropping oldest event")
                self._queue.popleft()
            self._queue.append(event)
            should_flush = len(self._queue) >= self._config.batch_size
        if should_flush:
            self._wake.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="apdl-flush", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._config.flush_interval + self._config.request_timeout)
        self._thread = None

    def flush(self) -> None:
        """Synchronously drains the queue, sending every pending batch.

        Stops early if a batch fails to send: the failed batch is re-queued at
        the front, so retrying it in the same loop would spin forever. The next
        timer tick (or ``flush`` call) picks it back up.
        """
        while True:
            batch = self._take_batch()
            if not batch:
                return
            if not self._send(batch):
                return

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._queue)

    # ── Internals ─────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._wake.wait(timeout=self._config.flush_interval)
            self._wake.clear()
            self.flush()
        # Final drain on shutdown.
        self.flush()

    def _take_batch(self) -> list[dict[str, Any]]:
        with self._lock:
            batch: list[dict[str, Any]] = []
            for _ in range(self._config.batch_size):
                if not self._queue:
                    break
                batch.append(self._queue.popleft())
            return batch

    def _send(self, batch: list[dict[str, Any]]) -> bool:
        """Sends a batch. On failure re-queues it at the front and returns False."""
        try:
            ok = self._transport.post_json(self._url, {"events": batch})
        except Exception:  # noqa: BLE001 - never let the flush thread die
            logger.exception("APDL: unexpected error during flush")
            ok = False

        if not ok:
            if self._config.debug:
                logger.warning("APDL: batch send failed, re-queuing %d events", len(batch))
            self._requeue_front(batch)
        return ok

    def _requeue_front(self, batch: list[dict[str, Any]]) -> None:
        with self._lock:
            self._queue.extendleft(reversed(batch))
            # Trim back down to the cap, dropping oldest.
            while len(self._queue) > self._config.max_queue_size:
                self._queue.popleft()
