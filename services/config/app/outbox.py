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
MAX_DELIVERY_ATTEMPTS = 8
READINESS_MAX_PENDING_AGE_SECONDS = 300.0
READINESS_MAX_QUARANTINED_ROWS = 0
FAILURE_EVIDENCE_MAX_CHARS = 2000
CLEANUP_BATCH_SIZE = 500
CLEANUP_INTERVAL_SECONDS = 300.0
PROCESSED_RETENTION_SECONDS = 7 * 24 * 60 * 60
QUARANTINED_RETENTION_SECONDS = 90 * 24 * 60 * 60
CLICKHOUSE_EVENT_RETENTION_MAX_SECONDS = 366 * 24 * 60 * 60
EXPOSURE_RECEIPT_RETENTION_SECONDS = 400 * 24 * 60 * 60
CLEANUP_READINESS_GRACE_SECONDS = 60 * 60

if not 0 < EVENT_STREAM_ALERT_ENTRIES < EVENT_STREAM_MAX_ENTRIES:
    raise RuntimeError("Event stream alert threshold must be below capacity")
if MAX_DELIVERY_ATTEMPTS < 1:
    raise RuntimeError("Config outbox delivery attempts must be positive")
if EXPOSURE_RECEIPT_RETENTION_SECONDS <= CLICKHOUSE_EVENT_RETENTION_MAX_SECONDS:
    raise RuntimeError("Exposure receipts must outlive ClickHouse events")

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


class PermanentDeliveryError(ValueError):
    """A persisted row cannot succeed without changing its canonical payload."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _payload(value) -> dict:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as exc:
        raise PermanentDeliveryError(
            "invalid_payload",
            "Config outbox payload is not valid JSON",
        ) from exc
    if not isinstance(parsed, dict):
        raise PermanentDeliveryError(
            "invalid_payload",
            "Config outbox payload must be an object",
        )
    return dict(parsed)


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
                      AND pending.quarantined_at IS NULL
                      AND pending.attempts < {MAX_DELIVERY_ATTEMPTS}
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
                            AND earlier.quarantined_at IS NULL
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
        "payload": row["payload"],
        "attempts": row["attempts"],
    }


async def mark_processed(pool, row_id: int) -> None:
    await pool.execute(
        """
        UPDATE config_outbox
        SET processed_at = now(), claimed_at = NULL, last_error = ''
        WHERE id = $1 AND processed_at IS NULL AND quarantined_at IS NULL
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
          AND quarantined_at IS NULL AND attempts < $4
        """,
        row_id,
        backoff,
        error[:FAILURE_EVIDENCE_MAX_CHARS],
        MAX_DELIVERY_ATTEMPTS,
    )


async def mark_quarantined(
    pool,
    row_id: int,
    *,
    failure_class: str,
    failure_code: str,
    error: str,
) -> None:
    await pool.execute(
        """
        UPDATE config_outbox
        SET quarantined_at = now(),
            claimed_at = NULL,
            failure_class = $2,
            failure_code = $3,
            last_error = $4
        WHERE id = $1 AND processed_at IS NULL AND quarantined_at IS NULL
        """,
        row_id,
        failure_class,
        failure_code,
        error[:FAILURE_EVIDENCE_MAX_CHARS],
    )


async def quarantine_exhausted(pool) -> int:
    """Terminalize capped rows, including a final attempt abandoned by a crash."""
    result = await pool.execute(
        """
        UPDATE config_outbox
        SET quarantined_at = now(),
            claimed_at = NULL,
            failure_class = 'attempts_exhausted',
            failure_code = 'delivery_attempts_exhausted',
            last_error = CASE
                WHEN last_error = '' THEN 'Delivery attempt limit reached'
                ELSE last_error
            END
        WHERE processed_at IS NULL
          AND quarantined_at IS NULL
          AND attempts >= $1
          AND (
              claimed_at IS NULL
              OR claimed_at < now() - ($2 * interval '1 second')
          )
        """,
        MAX_DELIVERY_ATTEMPTS,
        CLAIM_TIMEOUT_SECONDS,
    )
    try:
        return int(result.rsplit(" ", 1)[-1])
    except (AttributeError, ValueError):
        return 0


def _exact_payload_keys(payload: dict, expected: set[str]) -> None:
    if set(payload) != expected:
        raise PermanentDeliveryError(
            "invalid_payload",
            "Config outbox payload has noncanonical fields",
        )


def _config_delivery_payload(
    raw_payload,
    *,
    expected_event_type: str,
) -> tuple[dict, int, str]:
    payload = _payload(raw_payload)
    _exact_payload_keys(payload, {"event_type", "project_version", "data"})
    if payload["event_type"] != expected_event_type:
        raise PermanentDeliveryError(
            "invalid_payload",
            "Config outbox payload has an invalid event_type",
        )
    if not isinstance(payload["data"], dict):
        raise PermanentDeliveryError(
            "invalid_payload",
            "Config outbox data must be an object",
        )
    try:
        data_json = json.dumps(
            payload["data"],
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise PermanentDeliveryError(
            "invalid_payload",
            "Config outbox data is not canonical JSON",
        ) from exc
    return payload, _project_version(payload), data_json


def _exposure_delivery_payload(raw_payload, project_id: str) -> tuple[str, str]:
    payload = _payload(raw_payload)
    _exact_payload_keys(payload, {"stream_key", "event"})
    stream_key = payload["stream_key"]
    if stream_key != f"events:raw:{project_id}":
        raise PermanentDeliveryError(
            "invalid_payload",
            "Config exposure stream does not match its project",
        )
    event = payload["event"]
    if not isinstance(event, dict):
        raise PermanentDeliveryError(
            "invalid_payload",
            "Config exposure event must be an object",
        )
    required_event_fields = {
        "event",
        "type",
        "timestamp",
        "message_id",
        "session_id",
        "context",
        "properties",
    }
    optional_event_fields = {"user_id", "anonymous_id"}
    if not required_event_fields.issubset(event) or not set(event).issubset(
        required_event_fields | optional_event_fields
    ):
        raise PermanentDeliveryError(
            "invalid_payload",
            "Config exposure event has noncanonical fields",
        )
    if event["event"] != "$feature_flag_exposure" or event["type"] != "track":
        raise PermanentDeliveryError(
            "invalid_payload",
            "Config exposure event has an invalid type",
        )
    for field in ("timestamp", "message_id", "session_id"):
        if not isinstance(event[field], str) or not event[field]:
            raise PermanentDeliveryError(
                "invalid_payload",
                f"Config exposure event has an invalid {field}",
            )
    if not isinstance(event["context"], dict) or not isinstance(
        event["properties"], dict
    ):
        raise PermanentDeliveryError(
            "invalid_payload",
            "Config exposure context and properties must be objects",
        )
    identities = [event.get("user_id"), event.get("anonymous_id")]
    if not any(isinstance(value, str) and value for value in identities):
        raise PermanentDeliveryError(
            "invalid_payload",
            "Config exposure event requires an identity",
        )
    try:
        event_json = json.dumps(
            event,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise PermanentDeliveryError(
            "invalid_payload",
            "Config exposure event is not canonical JSON",
        ) from exc
    return stream_key, event_json


async def deliver(row: dict, redis, broadcaster) -> None:
    kind = row["kind"]
    project_id = row["project_id"]
    if kind == "flag_change":
        payload, project_version, data_json = _config_delivery_payload(
            row["payload"],
            expected_event_type="flag_update",
        )
        await redis_cache.invalidate_flags(redis, project_id, project_version)
        await broadcaster.broadcast(
            project_id,
            payload["event_type"],
            data_json,
            project_version=project_version,
        )
        return
    if kind == "experiment_change":
        payload, project_version, data_json = _config_delivery_payload(
            row["payload"],
            expected_event_type="experiment_update",
        )
        await redis_cache.invalidate_experiments(redis, project_id)
        await broadcaster.broadcast(
            project_id,
            payload["event_type"],
            data_json,
            project_version=project_version,
        )
        return
    if kind == "exposure":
        stream_key, event_json = _exposure_delivery_payload(
            row["payload"],
            project_id,
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
    raise PermanentDeliveryError(
        "unsupported_kind",
        f"Unsupported Config outbox kind: {kind}",
    )


def _project_version(payload: dict) -> int:
    project_version = payload.get("project_version")
    if (
        isinstance(project_version, bool)
        or not isinstance(project_version, int)
        or project_version < 1
    ):
        raise PermanentDeliveryError(
            "invalid_project_version",
            "Config outbox payload has an invalid project_version",
        )
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


def empty_metrics() -> dict:
    return {
        "pending_count": 0,
        "processed_count": 0,
        "quarantined_count": 0,
        "estimated_receipt_count": 0,
        "max_pending_attempts": 0,
        "oldest_pending_age_seconds": 0.0,
        "oldest_processed_age_seconds": 0.0,
        "oldest_quarantined_age_seconds": 0.0,
        "oldest_receipt_age_seconds": 0.0,
    }


async def metrics_snapshot(conn) -> dict:
    """Return bounded, low-cardinality delivery lag and quarantine metrics."""
    row = await conn.fetchrow(
        """
        WITH outbox_metrics AS (
            SELECT
                count(*) FILTER (
                    WHERE processed_at IS NULL AND quarantined_at IS NULL
                ) AS pending_count,
                count(*) FILTER (
                    WHERE processed_at IS NOT NULL
                ) AS processed_count,
                count(*) FILTER (
                    WHERE quarantined_at IS NOT NULL
                ) AS quarantined_count,
                COALESCE(max(attempts) FILTER (
                    WHERE processed_at IS NULL AND quarantined_at IS NULL
                ), 0) AS max_pending_attempts,
                COALESCE(EXTRACT(EPOCH FROM (
                    now() - (
                        min(created_at) FILTER (
                            WHERE processed_at IS NULL
                              AND quarantined_at IS NULL
                        )
                    )
                )), 0) AS oldest_pending_age_seconds,
                COALESCE(EXTRACT(EPOCH FROM (
                    now() - min(processed_at)
                )), 0) AS oldest_processed_age_seconds,
                COALESCE(EXTRACT(EPOCH FROM (
                    now() - min(quarantined_at)
                )), 0) AS oldest_quarantined_age_seconds
            FROM config_outbox
        ),
        receipt_metrics AS (
            SELECT COALESCE(EXTRACT(EPOCH FROM (
                now() - min(last_seen_at)
            )), 0) AS oldest_receipt_age_seconds
            FROM config_exposure_receipts
        )
        SELECT
            outbox_metrics.*,
            receipt_metrics.oldest_receipt_age_seconds,
            COALESCE((
                SELECT greatest(n_live_tup, 0)::bigint
                FROM pg_stat_user_tables
                WHERE schemaname = 'public'
                  AND relname = 'config_exposure_receipts'
            ), 0) AS estimated_receipt_count
        FROM outbox_metrics
        CROSS JOIN receipt_metrics
        """
    )
    return {
        "pending_count": int(row["pending_count"]),
        "processed_count": int(row["processed_count"]),
        "quarantined_count": int(row["quarantined_count"]),
        "estimated_receipt_count": int(row["estimated_receipt_count"]),
        "max_pending_attempts": int(row["max_pending_attempts"]),
        "oldest_pending_age_seconds": max(
            float(row["oldest_pending_age_seconds"]),
            0.0,
        ),
        "oldest_processed_age_seconds": max(
            float(row["oldest_processed_age_seconds"]),
            0.0,
        ),
        "oldest_quarantined_age_seconds": max(
            float(row["oldest_quarantined_age_seconds"]),
            0.0,
        ),
        "oldest_receipt_age_seconds": max(
            float(row["oldest_receipt_age_seconds"]),
            0.0,
        ),
    }


def readiness_snapshot(metrics: dict) -> dict:
    reasons: list[str] = []
    if (
        metrics["oldest_pending_age_seconds"]
        > READINESS_MAX_PENDING_AGE_SECONDS
    ):
        reasons.append("oldest_pending_age_exceeded")
    if metrics["quarantined_count"] > READINESS_MAX_QUARANTINED_ROWS:
        reasons.append("quarantined_rows_exceeded")
    if (
        metrics["oldest_processed_age_seconds"]
        > PROCESSED_RETENTION_SECONDS + CLEANUP_READINESS_GRACE_SECONDS
    ):
        reasons.append("processed_cleanup_overdue")
    if (
        metrics["oldest_quarantined_age_seconds"]
        > QUARANTINED_RETENTION_SECONDS + CLEANUP_READINESS_GRACE_SECONDS
    ):
        reasons.append("quarantined_cleanup_overdue")
    if (
        metrics["oldest_receipt_age_seconds"]
        > EXPOSURE_RECEIPT_RETENTION_SECONDS
        + CLEANUP_READINESS_GRACE_SECONDS
    ):
        reasons.append("receipt_cleanup_overdue")
    return {
        **metrics,
        "status": "degraded" if reasons else "ready",
        "degraded_reasons": reasons,
        "thresholds": {
            "max_pending_age_seconds": READINESS_MAX_PENDING_AGE_SECONDS,
            "max_quarantined_rows": READINESS_MAX_QUARANTINED_ROWS,
            "processed_retention_seconds": PROCESSED_RETENTION_SECONDS,
            "quarantined_retention_seconds": QUARANTINED_RETENTION_SECONDS,
            "exposure_receipt_retention_seconds": (
                EXPOSURE_RECEIPT_RETENTION_SECONDS
            ),
            "cleanup_readiness_grace_seconds": CLEANUP_READINESS_GRACE_SECONDS,
            "cleanup_batch_size": CLEANUP_BATCH_SIZE,
        },
    }


async def cleanup_once(pool) -> dict[str, int]:
    """Prune bounded terminal/receipt batches without blocking live workers."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            processed = await conn.fetch(
                """
                WITH candidates AS (
                    SELECT id
                    FROM config_outbox
                    WHERE processed_at < now() - ($2 * interval '1 second')
                    ORDER BY processed_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                )
                DELETE FROM config_outbox AS outbox
                USING candidates
                WHERE outbox.id = candidates.id
                RETURNING outbox.id
                """,
                CLEANUP_BATCH_SIZE,
                PROCESSED_RETENTION_SECONDS,
            )
            quarantined = await conn.fetch(
                """
                WITH candidates AS (
                    SELECT id
                    FROM config_outbox
                    WHERE quarantined_at < now() - ($2 * interval '1 second')
                    ORDER BY quarantined_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                )
                DELETE FROM config_outbox AS outbox
                USING candidates
                WHERE outbox.id = candidates.id
                RETURNING outbox.id
                """,
                CLEANUP_BATCH_SIZE,
                QUARANTINED_RETENTION_SECONDS,
            )
            receipts = await conn.fetch(
                """
                WITH candidates AS (
                    SELECT receipt.project_id, receipt.message_id
                    FROM config_exposure_receipts AS receipt
                    WHERE receipt.last_seen_at
                          < now() - ($2 * interval '1 second')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM config_outbox AS outbox
                          WHERE outbox.project_id = receipt.project_id
                            AND outbox.kind = 'exposure'
                            AND outbox.dedup_key = receipt.message_id
                      )
                    ORDER BY receipt.last_seen_at,
                             receipt.project_id,
                             receipt.message_id
                    FOR UPDATE OF receipt SKIP LOCKED
                    LIMIT $1
                )
                DELETE FROM config_exposure_receipts AS receipt
                USING candidates
                WHERE receipt.project_id = candidates.project_id
                  AND receipt.message_id = candidates.message_id
                RETURNING receipt.project_id
                """,
                CLEANUP_BATCH_SIZE,
                EXPOSURE_RECEIPT_RETENTION_SECONDS,
            )
    return {
        "processed": len(processed),
        "quarantined": len(quarantined),
        "receipts": len(receipts),
    }


def _error_evidence(exc: BaseException) -> str:
    message = str(exc).strip() or type(exc).__name__
    return message[:FAILURE_EVIDENCE_MAX_CHARS]


async def drain_once(pool, redis, broadcaster) -> bool:
    """Deliver at most one row. Returns whether a row was claimed."""
    await quarantine_exhausted(pool)
    row = await claim_next(pool)
    if row is None:
        return False
    try:
        await deliver(row, redis, broadcaster)
    except PermanentDeliveryError as exc:
        await mark_quarantined(
            pool,
            row["id"],
            failure_class="permanent",
            failure_code=exc.code,
            error=_error_evidence(exc),
        )
        logger.error(
            "Config outbox delivery %s quarantined permanently: %s",
            row["id"],
            exc,
            extra={
                "event": "config_outbox_quarantined",
                "outbox_id": row["id"],
                "project_id": row["project_id"],
                "kind": row["kind"],
                "attempts": row["attempts"],
                "failure_class": "permanent",
                "failure_code": exc.code,
            },
        )
    except Exception as exc:
        if row["attempts"] >= MAX_DELIVERY_ATTEMPTS:
            await mark_quarantined(
                pool,
                row["id"],
                failure_class="attempts_exhausted",
                failure_code="delivery_attempts_exhausted",
                error=_error_evidence(exc),
            )
            logger.error(
                "Config outbox delivery %s exhausted %s attempts: %s",
                row["id"],
                row["attempts"],
                exc,
                extra={
                    "event": "config_outbox_quarantined",
                    "outbox_id": row["id"],
                    "project_id": row["project_id"],
                    "kind": row["kind"],
                    "attempts": row["attempts"],
                    "failure_class": "attempts_exhausted",
                    "failure_code": "delivery_attempts_exhausted",
                },
            )
        else:
            await mark_failed(
                pool,
                row["id"],
                row["attempts"],
                _error_evidence(exc),
            )
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
    cleanup_interval_seconds: float = CLEANUP_INTERVAL_SECONDS,
) -> None:
    next_cleanup_at = 0.0
    while True:
        now = time.monotonic()
        if now >= next_cleanup_at:
            cleanup_backlog = False
            try:
                cleaned = await cleanup_once(pool)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Config outbox cleanup failed")
            else:
                cleanup_backlog = any(
                    count >= CLEANUP_BATCH_SIZE for count in cleaned.values()
                )
                if any(cleaned.values()):
                    logger.info(
                        "Config outbox cleanup pruned terminal rows",
                        extra={
                            "event": "config_outbox_cleanup",
                            **cleaned,
                        },
                    )
            next_cleanup_at = (
                now if cleanup_backlog else now + cleanup_interval_seconds
            )
        try:
            claimed = await drain_once(pool, redis, broadcaster)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Config outbox poll failed")
            claimed = False
        if not claimed:
            await asyncio.sleep(idle_seconds)
