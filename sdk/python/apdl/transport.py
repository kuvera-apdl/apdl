"""HTTP transport with typed outcomes and retry/backoff over httpx.

Mirrors the retry policy of ``sdk/javascript/src/core/transport.ts``: 2xx is
accepted; 408/425/429, 5xx, and network errors are retryable; and every other
final HTTP status is a permanent rejection. Retries use exponential backoff and
honor ``Retry-After`` on 429. The backoff schedule is shorter than the browser
SDK's since a blocked server flush thread should fail fast rather than stall
for minutes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import Any

import httpx

from .types import SDK_IDENTIFIER

logger = logging.getLogger("apdl")

DEFAULT_RETRY_BACKOFF: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0)
_MAX_RETRY_AFTER = 60.0
_RETRYABLE_CLIENT_STATUSES = frozenset({408, 425, 429})


class TransportOutcome(str, Enum):
    """Queue-facing classification for one fully attempted request."""

    ACCEPTED = "accepted"
    RETRYABLE = "retryable"
    PERMANENT_REJECTION = "permanent_rejection"


class Transport:
    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 10.0,
        debug: bool = False,
        backoff: tuple[float, ...] = DEFAULT_RETRY_BACKOFF,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._debug = debug
        self._backoff = backoff
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self._retry_cancelled = threading.Event()
        self._uses_default_sleep = sleep is time.sleep

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-API-Key": self._api_key,
            "X-APDL-SDK": SDK_IDENTIFIER,
        }

    def post_json(self, url: str, payload: Any) -> TransportOutcome:
        """POST JSON and classify the final result for queue disposition.

        A shutdown cancellation still permits the current/first HTTP attempt,
        but it prevents every later retry and backoff sleep. This gives the
        queue one bounded final delivery opportunity without closing a client
        beneath an active request.
        """
        attempts = len(self._backoff)
        for attempt in range(attempts + 1):
            try:
                response = self._client.post(url, json=payload, headers=self.headers)
            except (
                TypeError,
                ValueError,
                OverflowError,
                UnicodeError,
                RecursionError,
            ) as err:
                if self._debug:
                    logger.warning("APDL: permanently rejected JSON payload: %s", err)
                return TransportOutcome.PERMANENT_REJECTION
            except httpx.HTTPError as err:
                if self._debug:
                    logger.warning("APDL: network error on attempt %d: %s", attempt + 1, err)
            else:
                if response.is_success:
                    return TransportOutcome.ACCEPTED

                status = response.status_code
                if (
                    status not in _RETRYABLE_CLIENT_STATUSES
                    and not 500 <= status < 600
                ):
                    if self._debug:
                        logger.warning("APDL: non-retryable %d from %s", status, url)
                    return TransportOutcome.PERMANENT_REJECTION

                if self._debug:
                    logger.warning(
                        "APDL: retryable %d from %s, attempt %d", status, url, attempt + 1
                    )
                if self._retry_cancelled.is_set():
                    return TransportOutcome.RETRYABLE
                if status == 429 and self._honor_retry_after(response, attempt, attempts):
                    continue

            if attempt < attempts:
                if not self._wait_for_retry(self._backoff[attempt]):
                    return TransportOutcome.RETRYABLE

        return TransportOutcome.RETRYABLE

    def get_json(self, url: str) -> Any | None:
        """GETs JSON once (no retry). Returns parsed body or ``None``."""
        try:
            response = self._client.get(url, headers=self.headers)
        except httpx.HTTPError as err:
            if self._debug:
                logger.warning("APDL: flag fetch failed: %s", err)
            return None
        if not response.is_success:
            if self._debug:
                logger.warning("APDL: flag fetch returned %d", response.status_code)
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def cancel_retries(self) -> None:
        """Interrupt retry sleeps while allowing an in-flight request to finish."""
        self._retry_cancelled.set()

    def _honor_retry_after(
        self, response: httpx.Response, attempt: int, attempts: int
    ) -> bool:
        """Sleeps for ``Retry-After`` seconds if present; returns whether handled."""
        raw = response.headers.get("Retry-After")
        if raw is None:
            return False
        try:
            seconds = float(raw)
        except ValueError:
            return False
        if seconds <= 0 or attempt >= attempts:
            return False
        return self._wait_for_retry(min(seconds, _MAX_RETRY_AFTER))

    def _wait_for_retry(self, seconds: float) -> bool:
        """Return ``True`` only after a full, uncancelled retry delay."""
        if self._retry_cancelled.is_set():
            return False
        if self._uses_default_sleep:
            return not self._retry_cancelled.wait(seconds)
        self._sleep(seconds)
        return not self._retry_cancelled.is_set()
