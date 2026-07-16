"""At-least-once delivery for committed Config side effects.

The OSS preview supports one Config process. PostgreSQL claims still make a
crashed delivery retryable and keep network calls outside mutation requests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from app.store import redis_cache

logger = logging.getLogger(__name__)

EVENT_STREAM_MAX_ENTRIES = 1_000_000
EVENT_STREAM_ALERT_ENTRIES = 750_000
EVENT_STREAM_ALERT_LOG_INTERVAL_SECONDS = 30.0
_MAX_TRACKED_ALERT_STREAMS = 10_000
_last_alert_log: dict[tuple[str, str], float] = {}
CLAIM_TIMEOUT_SECONDS = 60
MAX_BACKOFF_SECONDS = 60

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


class EventStreamOverloaded(RuntimeError):
    """Raised when an exposure cannot be admitted without exceeding retention."""

    def __init__(self, stream_key: str, current_entries: int) -> None:
        self.stream_key = stream_key
        self.current_entries = current_entries
        super().__init__(
            f"Event stream {stream_key} is at its durability capacity "
            f"({current_entries}/{EVENT_STREAM_MAX_ENTRIES})"
        )


def _payload(value) -> dict:
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


async def claim_next(pool) -> dict | None:
    """Claim one due row; stale claims become available after one minute."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                WITH candidate AS (
                    SELECT pending.id
                    FROM config_outbox AS pending
                    WHERE pending.processed_at IS NULL
                      AND pending.available_at <= now()
                      AND (
                          pending.claimed_at IS NULL
                          OR pending.claimed_at < now() - interval '{CLAIM_TIMEOUT_SECONDS} seconds'
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM config_outbox AS earlier
                          WHERE earlier.project_id = pending.project_id
                            AND earlier.processed_at IS NULL
                            AND earlier.id < pending.id
                            AND (
                                (
                                    pending.kind IN (
                                        'flag_change', 'experiment_change'
                                    )
                                    AND earlier.kind IN (
                                        'flag_change', 'experiment_change'
                                    )
                                )
                                OR (
                                    pending.kind = 'exposure'
                                    AND earlier.kind = 'exposure'
                                )
                            )
                      )
                    ORDER BY pending.available_at, pending.id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE config_outbox AS outbox
                SET claimed_at = now(), attempts = attempts + 1
                FROM candidate
                WHERE outbox.id = candidate.id
                RETURNING outbox.id, outbox.project_id, outbox.kind,
                          outbox.payload, outbox.attempts
                """
            )
    if row is None:
        return None
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "kind": row["kind"],
        "payload": _payload(row["payload"]),
        "attempts": row["attempts"],
    }


async def mark_processed(pool, row_id: int) -> None:
    await pool.execute(
        """
        UPDATE config_outbox
        SET processed_at = now(), claimed_at = NULL, last_error = ''
        WHERE id = $1 AND processed_at IS NULL
        """,
        row_id,
    )


async def mark_failed(pool, row_id: int, attempts: int, error: str) -> None:
    backoff = min(2 ** max(attempts - 1, 0), MAX_BACKOFF_SECONDS)
    await pool.execute(
        """
        UPDATE config_outbox
        SET claimed_at = NULL,
            available_at = now() + ($2 * interval '1 second'),
            last_error = $3
        WHERE id = $1 AND processed_at IS NULL
        """,
        row_id,
        backoff,
        error[:2000],
    )


async def deliver(row: dict, redis, broadcaster) -> None:
    kind = row["kind"]
    project_id = row["project_id"]
    payload = row["payload"]
    if kind == "flag_change":
        project_version = _project_version(payload)
        await redis_cache.invalidate_flags(redis, project_id, project_version)
        await broadcaster.broadcast(
            project_id,
            payload["event_type"],
            json.dumps(payload["data"], separators=(",", ":")),
            project_version=project_version,
        )
        return
    if kind == "experiment_change":
        await redis_cache.invalidate_experiments(redis, project_id)
        await broadcaster.broadcast(
            project_id,
            payload["event_type"],
            json.dumps(payload["data"], separators=(",", ":")),
            project_version=_project_version(payload),
        )
        return
    if kind == "exposure":
        stream_key = payload["stream_key"]
        event_json = json.dumps(
            payload["event"],
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = await redis.eval(
            _BOUNDED_XADD_LUA,
            1,
            stream_key,
            1,
            EVENT_STREAM_MAX_ENTRIES,
            event_json,
        )
        if not isinstance(result, (list, tuple)) or len(result) < 2:
            raise RuntimeError("Redis bounded XADD returned an invalid response")

        admitted = int(result[0])
        stream_entries = int(result[1])
        log_context = {
            "event": (
                "event_stream_overloaded"
                if admitted == 0
                else "event_stream_pressure"
            ),
            "stream_key": stream_key,
            "outstanding_entries": stream_entries,
            "max_entries": EVENT_STREAM_MAX_ENTRIES,
            "alert_entries": EVENT_STREAM_ALERT_ENTRIES,
            "project_id": project_id,
            "outbox_id": row.get("id"),
        }
        if admitted == 0:
            if _should_log_alert("event_stream_overloaded", stream_key):
                logger.error(
                    "event_stream_overloaded stream=%s outstanding_entries=%s "
                    "max_entries=%s",
                    stream_key,
                    stream_entries,
                    EVENT_STREAM_MAX_ENTRIES,
                    extra=log_context,
                )
            raise EventStreamOverloaded(stream_key, stream_entries)
        if admitted != 1 or len(result) != 3:
            raise RuntimeError("Redis bounded XADD returned an invalid response")
        if (
            stream_entries >= EVENT_STREAM_ALERT_ENTRIES
            and _should_log_alert("event_stream_pressure", stream_key)
        ):
            logger.warning(
                "event_stream_pressure stream=%s outstanding_entries=%s "
                "max_entries=%s",
                stream_key,
                stream_entries,
                EVENT_STREAM_MAX_ENTRIES,
                extra=log_context,
            )
        return
    raise ValueError(f"Unsupported Config outbox kind: {kind}")


def _project_version(payload: dict) -> int:
    project_version = payload.get("project_version")
    if (
        isinstance(project_version, bool)
        or not isinstance(project_version, int)
        or project_version < 1
    ):
        raise ValueError("Config outbox payload has an invalid project_version")
    return project_version


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


async def drain_once(pool, redis, broadcaster) -> bool:
    """Deliver at most one row. Returns whether a row was claimed."""
    row = await claim_next(pool)
    if row is None:
        return False
    try:
        await deliver(row, redis, broadcaster)
    except Exception as exc:
        await mark_failed(pool, row["id"], row["attempts"], str(exc))
        logger.warning(
            "Config outbox delivery %s failed (attempt %s): %s",
            row["id"],
            row["attempts"],
            exc,
        )
    else:
        await mark_processed(pool, row["id"])
    return True


async def run_worker(
    pool,
    redis,
    broadcaster,
    *,
    idle_seconds: float = 0.25,
) -> None:
    while True:
        try:
            claimed = await drain_once(pool, redis, broadcaster)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Config outbox poll failed")
            claimed = False
        if not claimed:
            await asyncio.sleep(idle_seconds)
