"""At-least-once delivery for committed Config side effects.

The OSS preview supports one Config process. PostgreSQL claims still make a
crashed delivery retryable and keep network calls outside mutation requests.
"""

from __future__ import annotations

import asyncio
import json
import logging

from app.store import redis_cache

logger = logging.getLogger(__name__)

STREAM_MAXLEN = 1_000_000
CLAIM_TIMEOUT_SECONDS = 60
MAX_BACKOFF_SECONDS = 60


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
                            AND earlier.kind = pending.kind
                            AND earlier.processed_at IS NULL
                            AND earlier.id < pending.id
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
        await redis_cache.invalidate_flags(redis, project_id)
        await broadcaster.broadcast(
            project_id,
            payload["event_type"],
            json.dumps(payload["data"], separators=(",", ":")),
        )
        return
    if kind == "experiment_change":
        await redis_cache.invalidate_experiments(redis, project_id)
        await broadcaster.broadcast(
            project_id,
            payload["event_type"],
            json.dumps(payload["data"], separators=(",", ":")),
        )
        return
    if kind == "exposure":
        await redis.xadd(
            payload["stream_key"],
            {"event_json": json.dumps(payload["event"], separators=(",", ":"))},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        return
    raise ValueError(f"Unsupported Config outbox kind: {kind}")


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
