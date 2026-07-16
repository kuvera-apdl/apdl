"""Atomic, bounded Redis Streams publisher for canonical event batches."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

EVENT_STREAM_MAX_ENTRIES = 1_000_000
EVENT_STREAM_ALERT_ENTRIES = 750_000
EVENT_STREAM_RETRY_AFTER_SECONDS = 5
EVENT_STREAM_ALERT_LOG_INTERVAL_SECONDS = 30.0
_MAX_TRACKED_ALERT_STREAMS = 10_000
_last_alert_log: dict[tuple[str, str], float] = {}

if not 0 < EVENT_STREAM_ALERT_ENTRIES < EVENT_STREAM_MAX_ENTRIES:
    raise RuntimeError("Event stream alert threshold must be below capacity")

_BOUNDED_XADD_LUA = """
local count = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local current = redis.call('XLEN', KEYS[1])
if current + count > capacity then
  return {0, current}
end

local result = {1, current + count}
for index = 1, count do
  local id = redis.call('XADD', KEYS[1], '*', 'event_json', ARGV[index + 2])
  table.insert(result, id)
end
return result
"""


@dataclass(frozen=True)
class StreamOverloaded(RuntimeError):
    """The durable outstanding-entry boundary cannot admit a whole batch."""

    stream_key: str
    current_entries: int
    max_entries: int

    def __str__(self) -> str:
        return (
            f"Stream {self.stream_key!r} has {self.current_entries} outstanding "
            f"entries and capacity {self.max_entries}"
        )


async def publish_batch(redis, stream_key: str, events: list[dict]) -> list[str]:
    """Atomically publish a complete batch without trimming accepted events.

    ``XLEN`` represents outstanding work because the sole supported consumer
    group ACKs and deletes entries only after ClickHouse or the DLQ is durable.
    A connection failure remains ambiguous, so callers retry stable
    ``message_id`` values and storage converges at least once.
    """
    event_payloads = [
        json.dumps(
            event,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for event in events
    ]
    result = await redis.eval(
        _BOUNDED_XADD_LUA,
        1,
        stream_key,
        len(event_payloads),
        EVENT_STREAM_MAX_ENTRIES,
        *event_payloads,
    )
    if not isinstance(result, (list, tuple)) or len(result) < 2:
        raise RuntimeError("Redis admission script returned an invalid result")

    admitted = int(result[0])
    stream_entries = int(result[1])
    if admitted == 0:
        log_context = {
            "event": "event_stream_overloaded",
            "stream_key": stream_key,
            "outstanding_entries": stream_entries,
            "alert_entries": EVENT_STREAM_ALERT_ENTRIES,
            "max_entries": EVENT_STREAM_MAX_ENTRIES,
        }
        if _should_log_alert("event_stream_overloaded", stream_key):
            logger.error(
                "event_stream_overloaded stream=%s outstanding_entries=%d "
                "max_entries=%d",
                stream_key,
                stream_entries,
                EVENT_STREAM_MAX_ENTRIES,
                extra=log_context,
            )
        raise StreamOverloaded(
            stream_key=stream_key,
            current_entries=stream_entries,
            max_entries=EVENT_STREAM_MAX_ENTRIES,
        )
    if admitted != 1 or len(result) != len(event_payloads) + 2:
        raise RuntimeError("Redis admission script returned an incomplete result")

    message_ids = [_decode_message_id(value) for value in result[2:]]
    if (
        stream_entries >= EVENT_STREAM_ALERT_ENTRIES
        and _should_log_alert("event_stream_pressure", stream_key)
    ):
        log_context = {
            "event": "event_stream_pressure",
            "stream_key": stream_key,
            "outstanding_entries": stream_entries,
            "alert_entries": EVENT_STREAM_ALERT_ENTRIES,
            "max_entries": EVENT_STREAM_MAX_ENTRIES,
        }
        logger.warning(
            "event_stream_pressure stream=%s outstanding_entries=%d "
            "alert_entries=%d max_entries=%d",
            stream_key,
            stream_entries,
            EVENT_STREAM_ALERT_ENTRIES,
            EVENT_STREAM_MAX_ENTRIES,
            extra=log_context,
        )
    return message_ids


def _should_log_alert(event: str, stream_key: str) -> bool:
    now = time.monotonic()
    key = (event, stream_key)
    last_logged = _last_alert_log.get(key)
    if (
        last_logged is not None
        and now - last_logged < EVENT_STREAM_ALERT_LOG_INTERVAL_SECONDS
    ):
        return False
    if key not in _last_alert_log and len(_last_alert_log) >= _MAX_TRACKED_ALERT_STREAMS:
        oldest_key = min(_last_alert_log, key=_last_alert_log.get)
        del _last_alert_log[oldest_key]
    _last_alert_log[key] = now
    return True


def _decode_message_id(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii")
    if isinstance(value, str) and value:
        return value
    raise RuntimeError("Redis admission script returned an invalid message ID")
