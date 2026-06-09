"""HTTP transport with retry/backoff over httpx.

Mirrors the retry policy of ``sdk/javascript/src/core/transport.ts``: 2xx is
success, 4xx (except 429) is a permanent failure, and 429/5xx/network errors are
retried with exponential backoff honoring ``Retry-After`` on 429. The backoff
schedule is shorter than the browser SDK's since a blocked server flush thread
should fail fast rather than stall for minutes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from .types import SDK_IDENTIFIER

logger = logging.getLogger("apdl")

DEFAULT_RETRY_BACKOFF: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0)
_MAX_RETRY_AFTER = 60.0


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

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-API-Key": self._api_key,
            "X-APDL-SDK": SDK_IDENTIFIER,
        }

    def post_json(self, url: str, payload: Any) -> bool:
        """POSTs JSON with retry. Returns ``True`` only on a 2xx response."""
        attempts = len(self._backoff)
        for attempt in range(attempts + 1):
            try:
                response = self._client.post(url, json=payload, headers=self.headers)
            except httpx.HTTPError as err:
                if self._debug:
                    logger.warning("APDL: network error on attempt %d: %s", attempt + 1, err)
            else:
                if response.is_success:
                    return True

                status = response.status_code
                if 400 <= status < 500 and status != 429:
                    if self._debug:
                        logger.warning("APDL: non-retryable %d from %s", status, url)
                    return False

                if self._debug:
                    logger.warning(
                        "APDL: retryable %d from %s, attempt %d", status, url, attempt + 1
                    )
                if status == 429 and self._honor_retry_after(response, attempt, attempts):
                    continue

            if attempt < attempts:
                self._sleep(self._backoff[attempt])

        return False

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
        if seconds > 0 and attempt < attempts:
            self._sleep(min(seconds, _MAX_RETRY_AFTER))
        return True
