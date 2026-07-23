#!/usr/bin/env python3
"""Live PostgreSQL + Redis contract for fair boundary-marker publication."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import redis.asyncio as redis


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline" / "redis"))

import clickhouse_writer as writer_module  # noqa: E402


def _token(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _new_writer(pool: asyncpg.Pool, redis_client) -> object:
    writer = object.__new__(writer_module.ClickHouseWriter)
    writer.authority_pool = pool
    writer.redis_client = redis_client
    writer.stats = {
        "boundary_markers_published": 0,
        "boundary_markers_retried": 0,
        "boundary_markers_quarantined": 0,
        "errors": 0,
    }
    writer._durable_pending_ack = {}
    writer._boundary_tokens_by_delivery = {}
    writer._uncertain_redis_finalizations = set()
    writer._finalized_since_frontier = {}
    writer._flush_retry_count = 0
    writer._next_flush_retry_at = 0.0
    writer.durable_ack_authority_timeout = 0.25
    writer.buffer = []
    writer.buffer_size = 100
    return writer


async def _seed_boundaries(pool: asyncpg.Pool) -> dict[str, str]:
    tokens = {
        label: _token(label)
        for label in (
            "orphan",
            "healthy",
            "poison",
            "stale",
            "shared_owner",
            "shared_collision",
            "shared_later",
        )
    }
    rows = [
        (
            "orphan",
            "fails_after_xadd",
            "events:raw:orphan",
            tokens["orphan"],
            datetime(2025, 1, 1, tzinfo=UTC),
        ),
        (
            "healthy",
            "continues",
            "events:raw:healthy",
            tokens["healthy"],
            datetime(2025, 1, 2, tzinfo=UTC),
        ),
        (
            "poison",
            "wrong_entry",
            "events:raw:poison",
            tokens["poison"],
            datetime(2025, 1, 3, tzinfo=UTC),
        ),
        (
            "stale",
            "deleted_entry",
            "events:raw:stale",
            tokens["stale"],
            datetime(2025, 1, 4, tzinfo=UTC),
        ),
        (
            "shared",
            "first_owner",
            "events:raw:shared",
            tokens["shared_owner"],
            datetime(2025, 1, 5, tzinfo=UTC),
        ),
        (
            "shared",
            "colliding_poison",
            "events:raw:shared",
            tokens["shared_collision"],
            datetime(2025, 1, 6, tzinfo=UTC),
        ),
        (
            "shared",
            "later_progress",
            "events:raw:shared",
            tokens["shared_later"],
            datetime(2025, 1, 7, tzinfo=UTC),
        ),
    ]
    async with pool.acquire() as connection:
        await connection.executemany(
            """
            INSERT INTO experiment_analysis_boundaries (
                project_id,
                experiment_key,
                config_version,
                stream_key,
                window_start,
                window_end,
                marker_token,
                requested_at
            )
            VALUES (
                $1,
                $2,
                1,
                $3,
                TIMESTAMPTZ '2025-01-01 00:00:00+00',
                TIMESTAMPTZ '2025-01-31 00:00:00+00',
                $4,
                $5
            )
            """,
            rows,
        )
        await connection.execute(
            """
            CREATE FUNCTION reject_test_orphan_publication()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $reject_test_orphan_publication$
            BEGIN
                IF NEW.project_id = 'orphan'
                   AND NEW.marker_publish_state = 'published'
                THEN
                    RAISE EXCEPTION 'injected authority publication failure';
                END IF;
                RETURN NEW;
            END;
            $reject_test_orphan_publication$;

            CREATE TRIGGER reject_test_orphan_publication
            BEFORE UPDATE ON experiment_analysis_boundaries
            FOR EACH ROW
            EXECUTE FUNCTION reject_test_orphan_publication();
            """
        )
    return tokens


async def _assert_schema_gate_rejects_drift(pool: asyncpg.Pool) -> None:
    drift_statements = (
        """
        ALTER TABLE experiment_analysis_boundaries
            DROP CONSTRAINT
                experiment_analysis_boundaries_publish_attempts_check;
        ALTER TABLE experiment_analysis_boundaries
            ADD CONSTRAINT
                experiment_analysis_boundaries_publish_attempts_check
            CHECK (marker_publish_attempts >= 0);
        """,
        """
        ALTER TABLE experiment_analysis_boundaries
            DISABLE TRIGGER experiment_analysis_boundaries_immutable;
        """,
        """
        CREATE OR REPLACE FUNCTION
            enforce_experiment_analysis_boundary_immutability()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $weakened_boundary_state_machine$
        BEGIN
            RETURN NEW;
        END;
        $weakened_boundary_state_machine$;
        """,
    )
    async with pool.acquire() as connection:
        for statement in drift_statements:
            transaction = connection.transaction()
            await transaction.start()
            try:
                await connection.execute(statement)
                try:
                    await writer_module._assert_boundary_marker_schema(
                        connection
                    )
                except RuntimeError as exc:
                    assert "is not exact" in str(exc)
                else:
                    raise AssertionError(
                        "boundary marker schema drift passed startup gate"
                    )
            finally:
                await transaction.rollback()


async def _fetch_state(pool: asyncpg.Pool, project_id: str, key: str):
    return await pool.fetchrow(
        """
        SELECT
            marker_publish_state,
            marker_publish_attempts,
            marker_publish_failure_code,
            marker_publish_next_attempt_at,
            marker_publish_last_error_at,
            marker_publish_quarantined_at,
            marker_publish_observed_stream_id,
            marker_stream_id,
            EXTRACT(
                EPOCH FROM (
                    marker_publish_next_attempt_at
                    - marker_publish_last_error_at
                )
            )::double precision AS retry_delay_seconds
        FROM experiment_analysis_boundaries
        WHERE project_id = $1
          AND experiment_key = $2
          AND config_version = 1
        """,
        project_id,
        key,
    )


def _assert_state(
    row,
    *,
    state: str,
    attempts: int,
    failure_code: str | None,
    observed_stream_id: str | None,
) -> None:
    assert row is not None
    assert row["marker_publish_state"] == state
    assert row["marker_publish_attempts"] == attempts
    assert row["marker_publish_failure_code"] == failure_code
    assert row["marker_publish_observed_stream_id"] == observed_stream_id
    if state == "pending":
        assert row["marker_publish_next_attempt_at"] is not None
        assert row["marker_publish_quarantined_at"] is None
        assert row["marker_stream_id"] is None
    elif state == "published":
        assert row["marker_publish_next_attempt_at"] is None
        assert row["marker_publish_quarantined_at"] is None
        assert row["marker_stream_id"] == observed_stream_id
    else:
        assert state == "quarantined"
        assert row["marker_publish_next_attempt_at"] is None
        assert row["marker_publish_quarantined_at"] is not None
        assert row["marker_stream_id"] is None


async def _wait_until_due(row) -> None:
    deadline = row["marker_publish_next_attempt_at"]
    assert deadline is not None
    wait_seconds = max(0.0, (deadline - datetime.now(UTC)).total_seconds())
    await asyncio.sleep(wait_seconds + 0.1)


async def _assert_terminal_immutability(pool: asyncpg.Pool) -> None:
    try:
        await pool.execute(
            """
            UPDATE experiment_analysis_boundaries
            SET marker_publish_attempts = 4
            WHERE project_id = 'orphan'
              AND experiment_key = 'fails_after_xadd'
              AND config_version = 1
            """
        )
    except asyncpg.PostgresError as exc:
        assert "publication is terminal" in str(exc)
    else:
        raise AssertionError("terminal boundary publication state was mutable")


async def _consume_quarantined_marker(
    writer,
    pool: asyncpg.Pool,
    redis_client,
    *,
    project_id: str,
    marker_stream_id: str,
    delivered_token: str,
) -> None:
    stream_key = f"events:raw:{project_id}"
    deliveries = await redis_client.xreadgroup(
        groupname=writer_module.CONSUMER_GROUP,
        consumername="live-boundary-test",
        streams={stream_key: ">"},
        count=10,
        block=1000,
    )
    assert len(deliveries) == 1, repr(deliveries)
    assert deliveries[0][0] == stream_key
    assert deliveries[0][1] == [
        (
            marker_stream_id,
            {
                "message_kind": writer_module.BOUNDARY_MARKER_KIND,
                "boundary_token": delivered_token,
            },
        )
    ]
    writer._queue_durable_ack(
        [
            writer_module.BufferedEvent(
                stream_key=stream_key,
                message_id=marker_stream_id,
                row=None,
                boundary_token=delivered_token,
            )
        ]
    )

    assert await writer._ack_durable_messages() is True
    assert writer._durable_pending_ack == {}
    assert await redis_client.xlen(stream_key) == 0
    pending = await redis_client.xpending(
        stream_key,
        writer_module.CONSUMER_GROUP,
    )
    assert pending["pending"] == 0
    watermark = await pool.fetchrow(
        """
        SELECT status, failure_reason
        FROM event_pipeline_watermarks
        WHERE project_id = $1
        """,
        project_id,
    )
    assert dict(watermark) == {
        "status": "degraded",
        "failure_reason": "stream_state_unverifiable",
    }


async def _prove_blocked_stream_does_not_stall_healthy_ack(
    writer,
    pool: asyncpg.Pool,
    redis_client,
    *,
    blocked_stream_id: str,
    blocked_token: str,
    healthy_stream_id: str,
    healthy_token: str,
) -> None:
    deliveries = await redis_client.xreadgroup(
        groupname=writer_module.CONSUMER_GROUP,
        consumername="live-boundary-test",
        streams={
            "events:raw:orphan": ">",
            "events:raw:healthy": ">",
        },
        count=10,
        block=1000,
    )
    observed = {
        stream_key: messages
        for stream_key, messages in deliveries
    }
    assert observed == {
        "events:raw:healthy": [
            (
                healthy_stream_id,
                {
                    "message_kind": writer_module.BOUNDARY_MARKER_KIND,
                    "boundary_token": healthy_token,
                },
            )
        ],
        "events:raw:orphan": [
            (
                blocked_stream_id,
                {
                    "message_kind": writer_module.BOUNDARY_MARKER_KIND,
                    "boundary_token": blocked_token,
                },
            )
        ],
    }
    writer._queue_durable_ack(
        [
            writer_module.BufferedEvent(
                stream_key="events:raw:orphan",
                message_id=blocked_stream_id,
                row=None,
                boundary_token=blocked_token,
            ),
            writer_module.BufferedEvent(
                stream_key="events:raw:healthy",
                message_id=healthy_stream_id,
                row=None,
                boundary_token=healthy_token,
            ),
        ]
    )

    lock_connection = await pool.acquire()
    lock_transaction = lock_connection.transaction()
    await lock_transaction.start()
    try:
        await lock_connection.fetchrow(
            """
            SELECT project_id
            FROM event_pipeline_watermarks
            WHERE project_id = 'orphan'
            FOR UPDATE
            """
        )
        assert await writer._ack_durable_messages() is False
        assert writer._durable_pending_ack == {
            "events:raw:orphan": [blocked_stream_id]
        }
        assert writer._delivery_is_backpressured() is False
        assert writer._delivery_is_backpressured(
            "events:raw:orphan"
        ) is True
        assert writer._delivery_is_backpressured(
            "events:raw:healthy"
        ) is False
        assert await redis_client.xlen("events:raw:healthy") == 0
        healthy_pending = await redis_client.xpending(
            "events:raw:healthy",
            writer_module.CONSUMER_GROUP,
        )
        assert healthy_pending["pending"] == 0
    finally:
        await lock_transaction.rollback()
        await pool.release(lock_connection)

    assert await writer._ack_durable_messages() is True
    assert writer._durable_pending_ack == {}
    blocked_pending = await redis_client.xpending(
        "events:raw:orphan",
        writer_module.CONSUMER_GROUP,
    )
    assert blocked_pending["pending"] == 0
    blocked_watermark = await pool.fetchrow(
        """
        SELECT status, failure_reason
        FROM event_pipeline_watermarks
        WHERE project_id = 'orphan'
        """
    )
    assert dict(blocked_watermark) == {
        "status": "degraded",
        "failure_reason": "stream_state_unverifiable",
    }


async def _run(redis_url: str, postgres_url: str) -> None:
    pool = await asyncpg.create_pool(postgres_url, min_size=1, max_size=3)
    redis_client = redis.from_url(redis_url, decode_responses=True)
    writer = _new_writer(pool, redis_client)

    try:
        async with pool.acquire() as connection:
            await writer_module._assert_boundary_marker_schema(connection)
        await _assert_schema_gate_rejects_drift(pool)
        tokens = await _seed_boundaries(pool)

        # Establish a healthy consumer-group frontier before the injected
        # post-XADD PostgreSQL failures create the orphan marker.
        await writer._ensure_consumer_group("events:raw:orphan")
        await writer._ensure_consumer_group("events:raw:healthy")
        await writer._ensure_consumer_group("events:raw:poison")

        delivered_poison_token = _token("different-boundary")
        poison_id = await redis_client.xadd(
            "events:raw:poison",
            {
                "message_kind": writer_module.BOUNDARY_MARKER_KIND,
                "boundary_token": delivered_poison_token,
            },
        )
        await redis_client.set(
            f"{writer_module.BOUNDARY_MARKER_DEDUP_PREFIX}{tokens['poison']}",
            poison_id,
        )
        stale_id = await redis_client.xadd(
            "events:raw:stale",
            {
                "message_kind": writer_module.BOUNDARY_MARKER_KIND,
                "boundary_token": tokens["stale"],
            },
        )
        await redis_client.set(
            f"{writer_module.BOUNDARY_MARKER_DEDUP_PREFIX}{tokens['stale']}",
            stale_id,
        )
        assert await redis_client.xdel("events:raw:stale", stale_id) == 1

        # The orphan's XADD succeeds but its authority update fails. Per-marker
        # isolation must still publish the healthy tenant and terminally reject
        # both a valid-shaped poison ID and a deleted/stale dedup ID.
        await writer._publish_pending_boundary_markers(None)

        orphan = await _fetch_state(pool, "orphan", "fails_after_xadd")
        orphan_id = orphan["marker_publish_observed_stream_id"]
        assert orphan_id is not None
        _assert_state(
            orphan,
            state="pending",
            attempts=1,
            failure_code="boundary_authority_update_failed",
            observed_stream_id=orphan_id,
        )
        assert orphan["retry_delay_seconds"] == 1.0

        healthy = await _fetch_state(pool, "healthy", "continues")
        _assert_state(
            healthy,
            state="published",
            attempts=0,
            failure_code=None,
            observed_stream_id=healthy["marker_stream_id"],
        )
        healthy_id = healthy["marker_stream_id"]
        assert healthy_id is not None
        owner = await _fetch_state(pool, "shared", "first_owner")
        owner_id = owner["marker_stream_id"]
        _assert_state(
            owner,
            state="published",
            attempts=0,
            failure_code=None,
            observed_stream_id=owner_id,
        )
        assert owner_id is not None
        for project_id, key in (
            ("poison", "wrong_entry"),
            ("stale", "deleted_entry"),
        ):
            poisoned = await _fetch_state(pool, project_id, key)
            _assert_state(
                poisoned,
                state="quarantined",
                attempts=1,
                failure_code="invalid_boundary_marker_dedup",
                observed_stream_id=(
                    poison_id if project_id == "poison" else stale_id
                ),
            )
        assert await redis_client.xlen("events:raw:poison") == 1
        assert await redis_client.xlen("events:raw:stale") == 0

        # A poisoned dedup for boundary B points at boundary A's already-owned
        # ID in the same project. The unique authority collision must not roll
        # back B's terminal quarantine, and later boundary C must still publish.
        await redis_client.set(
            (
                f"{writer_module.BOUNDARY_MARKER_DEDUP_PREFIX}"
                f"{tokens['shared_collision']}"
            ),
            owner_id,
        )
        await writer._publish_pending_boundary_markers(["shared"])
        collision = await _fetch_state(
            pool,
            "shared",
            "colliding_poison",
        )
        _assert_state(
            collision,
            state="quarantined",
            attempts=1,
            failure_code="invalid_boundary_marker_dedup",
            observed_stream_id=None,
        )
        await writer._publish_pending_boundary_markers(["shared"])
        later = await _fetch_state(pool, "shared", "later_progress")
        _assert_state(
            later,
            state="published",
            attempts=0,
            failure_code=None,
            observed_stream_id=later["marker_stream_id"],
        )
        assert later["marker_stream_id"] != owner_id
        assert await redis_client.xlen("events:raw:shared") == 2

        # Exercise every persisted retry deadline. Redis must return and verify
        # the same exact marker ID on every idempotent publication attempt.
        for expected_attempt, expected_delay in ((2, 2.0), (3, 4.0), (4, 8.0)):
            await _wait_until_due(orphan)
            await writer._publish_pending_boundary_markers(["orphan"])
            orphan = await _fetch_state(pool, "orphan", "fails_after_xadd")
            _assert_state(
                orphan,
                state="pending",
                attempts=expected_attempt,
                failure_code="boundary_authority_update_failed",
                observed_stream_id=orphan_id,
            )
            assert orphan["retry_delay_seconds"] == expected_delay
            assert await redis_client.xlen("events:raw:orphan") == 1

        await _wait_until_due(orphan)
        await writer._publish_pending_boundary_markers(["orphan"])
        orphan = await _fetch_state(pool, "orphan", "fails_after_xadd")
        _assert_state(
            orphan,
            state="quarantined",
            attempts=5,
            failure_code="boundary_authority_update_failed",
            observed_stream_id=orphan_id,
        )
        assert orphan["marker_publish_last_error_at"] is not None
        assert (
            orphan["marker_publish_quarantined_at"]
            == orphan["marker_publish_last_error_at"]
        )
        await _assert_terminal_immutability(pool)

        # A quarantined post-XADD marker is consumable only after its exact
        # observed ID/token is reconciled and the project frontier is degraded.
        # Its durable-ACK queue must then clear rather than globally backpressure
        # all tenants forever.
        await _prove_blocked_stream_does_not_stall_healthy_ack(
            writer,
            pool,
            redis_client,
            blocked_stream_id=orphan_id,
            blocked_token=tokens["orphan"],
            healthy_stream_id=healthy_id,
            healthy_token=tokens["healthy"],
        )
        # The valid-shaped poison entry carries a different token, but its
        # atomically observed stream ID is enough to degrade and finalize it
        # without letting it become a second durable-ACK stall.
        await _consume_quarantined_marker(
            writer,
            pool,
            redis_client,
            project_id="poison",
            marker_stream_id=poison_id,
            delivered_token=delivered_poison_token,
        )

        assert writer.stats == {
            "boundary_markers_published": 3,
            "boundary_markers_retried": 4,
            "boundary_markers_quarantined": 4,
            "errors": 9,
        }
    finally:
        await redis_client.aclose()
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--postgres-url", required=True)
    args = parser.parse_args()
    asyncio.run(_run(args.redis_url, args.postgres_url))
    print("Boundary marker fairness/retry/quarantine contract passed")


if __name__ == "__main__":
    main()
