"""Durable Config outbox delivery and retry tests."""

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest

from app import outbox


@pytest.fixture(autouse=True)
def clear_alert_log_state():
    outbox._last_alert_log.clear()


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class RecordingClaimConn:
    def __init__(self):
        self.sql = ""

    def transaction(self):
        return _Context(None)

    async def fetchrow(self, sql: str):
        self.sql = sql
        return None


class RecordingClaimPool:
    def __init__(self):
        self.conn = RecordingClaimConn()

    def acquire(self):
        return _Context(self.conn)


class RecordingCleanupConn:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def transaction(self):
        return _Context(None)

    async def fetch(self, sql: str, *args):
        self.calls.append((sql, args))
        return [{"id": 1}, {"id": 2}]


class RecordingCleanupPool:
    def __init__(self):
        self.conn = RecordingCleanupConn()

    def acquire(self):
        return _Context(self.conn)


def exposure_row(*, attempts: int) -> dict:
    return {
        "id": 41,
        "project_id": "apdl",
        "kind": "exposure",
        "attempts": attempts,
        "payload": {
            "stream_key": "events:raw:apdl",
            "event": {
                "event": "$feature_flag_exposure",
                "type": "track",
                "timestamp": "2026-07-22T10:00:00Z",
                "message_id": "eval-1",
                "session_id": "server:eval-1",
                "user_id": "user-1",
                "context": {"library": {"name": "apdl-config"}},
                "properties": {"flag_key": "checkout"},
            },
        },
    }


@pytest.mark.asyncio
async def test_redis_failure_is_recorded_then_same_outbox_row_retries(
    monkeypatch,
):
    claim = AsyncMock(side_effect=[exposure_row(attempts=1), exposure_row(attempts=2)])
    failed = AsyncMock()
    processed = AsyncMock()
    monkeypatch.setattr(outbox, "claim_next", claim)
    monkeypatch.setattr(outbox, "mark_failed", failed)
    monkeypatch.setattr(outbox, "mark_processed", processed)
    monkeypatch.setattr(outbox, "quarantine_exhausted", AsyncMock(return_value=0))

    redis = AsyncMock()
    redis.eval = AsyncMock(
        side_effect=[RuntimeError("redis down"), [1, 1, b"1234567890-0"]]
    )
    pool = object()
    broadcaster = AsyncMock()

    assert await outbox.drain_once(pool, redis, broadcaster) is True
    failed.assert_awaited_once_with(pool, 41, 1, "redis down")
    processed.assert_not_awaited()

    assert await outbox.drain_once(pool, redis, broadcaster) is True
    processed.assert_awaited_once_with(pool, 41)
    assert redis.eval.await_count == 2


@pytest.mark.asyncio
async def test_exposure_uses_atomic_bounded_stream_admission():
    redis = AsyncMock()
    redis.eval.return_value = [1, 42, b"1234567890-0"]
    row = exposure_row(attempts=1)

    await outbox.deliver(row, redis, AsyncMock())

    event_json = json.dumps(
        row["payload"]["event"],
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    redis.eval.assert_awaited_once_with(
        outbox._BOUNDED_XADD_LUA,
        1,
        "events:raw:apdl",
        1,
        outbox.EVENT_STREAM_MAX_ENTRIES,
        event_json,
    )
    redis.xadd.assert_not_awaited()
    assert "XLEN" in outbox._BOUNDED_XADD_LUA
    assert "MAXLEN" not in outbox._BOUNDED_XADD_LUA


@pytest.mark.asyncio
async def test_stream_overload_keeps_outbox_row_pending_for_retry(
    monkeypatch,
    caplog,
):
    claim = AsyncMock(return_value=exposure_row(attempts=3))
    failed = AsyncMock()
    processed = AsyncMock()
    monkeypatch.setattr(outbox, "claim_next", claim)
    monkeypatch.setattr(outbox, "mark_failed", failed)
    monkeypatch.setattr(outbox, "mark_processed", processed)
    monkeypatch.setattr(outbox, "quarantine_exhausted", AsyncMock(return_value=0))
    redis = AsyncMock()
    redis.eval.return_value = [0, outbox.EVENT_STREAM_MAX_ENTRIES]

    with caplog.at_level(logging.ERROR, logger=outbox.__name__):
        assert await outbox.drain_once(object(), redis, AsyncMock()) is True

    processed.assert_not_awaited()
    failed.assert_awaited_once()
    pool, row_id, attempts, error = failed.await_args.args
    assert row_id == 41
    assert attempts == 3
    assert "durability capacity" in error
    overload = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "event_stream_overloaded"
    )
    assert overload.stream_key == "events:raw:apdl"
    assert overload.outstanding_entries == outbox.EVENT_STREAM_MAX_ENTRIES
    assert overload.max_entries == outbox.EVENT_STREAM_MAX_ENTRIES


@pytest.mark.asyncio
async def test_stream_pressure_emits_structured_alert(caplog):
    redis = AsyncMock()
    redis.eval.return_value = [
        1,
        outbox.EVENT_STREAM_ALERT_ENTRIES,
        b"1234567890-0",
    ]

    with caplog.at_level(logging.WARNING, logger=outbox.__name__):
        await outbox.deliver(exposure_row(attempts=1), redis, AsyncMock())

    pressure = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "event_stream_pressure"
    )
    assert pressure.outstanding_entries == outbox.EVENT_STREAM_ALERT_ENTRIES
    assert pressure.alert_entries == outbox.EVENT_STREAM_ALERT_ENTRIES
    assert pressure.project_id == "apdl"
    assert pressure.outbox_id == 41


@pytest.mark.asyncio
async def test_failed_head_preserves_global_config_order_per_project():
    pool = RecordingClaimPool()

    assert await outbox.claim_next(pool) is None

    sql = pool.conn.sql
    assert "NOT EXISTS" in sql
    assert "earlier.project_id = pending.project_id" in sql
    assert "pending.kind IN" in sql
    assert "'flag_change', 'experiment_change'" in sql
    assert "earlier.kind IN" in sql
    assert "pending.kind = 'exposure'" in sql
    assert "earlier.kind = 'exposure'" in sql
    assert "earlier.processed_at IS NULL" in sql
    assert "earlier.quarantined_at IS NULL" in sql
    assert "pending.quarantined_at IS NULL" in sql
    assert f"pending.attempts < {outbox.MAX_DELIVERY_ATTEMPTS}" in sql
    assert "earlier.id < pending.id" in sql
    # A failed config head blocks later flag and experiment rows for only that
    # project. Exposure durability remains independent from config delivery.
    assert "earlier.available_at" not in sql


@pytest.mark.asyncio
async def test_flag_change_invalidates_then_broadcasts_versioned_payload(monkeypatch):
    invalidate = AsyncMock()
    monkeypatch.setattr(outbox.redis_cache, "invalidate_flags", invalidate)
    broadcaster = AsyncMock()
    row = {
        "project_id": "apdl",
        "kind": "flag_change",
        "payload": {
            "event_type": "flag_update",
            "project_version": 19,
            "data": {
                "action": "flag_updated",
                "key": "checkout",
                "version": 7,
            },
        },
    }
    redis = object()

    await outbox.deliver(row, redis, broadcaster)

    invalidate.assert_awaited_once_with(redis, "apdl", 19)
    project_id, event_type, raw = broadcaster.broadcast.await_args.args
    assert project_id == "apdl"
    assert event_type == "flag_update"
    assert json.loads(raw)["version"] == 7
    assert broadcaster.broadcast.await_args.kwargs == {"project_version": 19}


@pytest.mark.asyncio
async def test_config_change_with_invalid_project_version_fails_closed(monkeypatch):
    monkeypatch.setattr(outbox.redis_cache, "invalidate_flags", AsyncMock())
    row = {
        "project_id": "apdl",
        "kind": "flag_change",
        "payload": {
            "event_type": "flag_update",
            "project_version": "19",
            "data": {},
        },
    }

    with pytest.raises(ValueError, match="project_version"):
        await outbox.deliver(row, object(), AsyncMock())


@pytest.mark.asyncio
async def test_no_due_outbox_row_is_idle(monkeypatch):
    monkeypatch.setattr(outbox, "claim_next", AsyncMock(return_value=None))
    monkeypatch.setattr(outbox, "quarantine_exhausted", AsyncMock(return_value=0))

    assert await outbox.drain_once(object(), object(), object()) is False


@pytest.mark.asyncio
async def test_unknown_outbox_kind_fails_closed():
    with pytest.raises(ValueError, match="Unsupported"):
        await outbox.deliver(
            {"project_id": "apdl", "kind": "unknown", "payload": {}},
            object(),
            object(),
        )


@pytest.mark.asyncio
async def test_malformed_payload_is_quarantined_as_permanent(monkeypatch):
    row = exposure_row(attempts=1)
    row["payload"] = {"event": {}}
    pool = object()
    quarantine = AsyncMock()
    failed = AsyncMock()
    processed = AsyncMock()
    monkeypatch.setattr(outbox, "claim_next", AsyncMock(return_value=row))
    monkeypatch.setattr(outbox, "mark_quarantined", quarantine)
    monkeypatch.setattr(outbox, "mark_failed", failed)
    monkeypatch.setattr(outbox, "mark_processed", processed)
    monkeypatch.setattr(outbox, "quarantine_exhausted", AsyncMock(return_value=0))

    assert await outbox.drain_once(pool, object(), object()) is True

    quarantine.assert_awaited_once_with(
        pool,
        41,
        failure_class="permanent",
        failure_code="invalid_payload",
        error="Config outbox payload has noncanonical fields",
    )
    failed.assert_not_awaited()
    processed.assert_not_awaited()


@pytest.mark.asyncio
async def test_retryable_failure_at_attempt_cap_is_quarantined(monkeypatch):
    row = exposure_row(attempts=outbox.MAX_DELIVERY_ATTEMPTS)
    pool = object()
    redis = AsyncMock()
    redis.eval.side_effect = RuntimeError("redis unavailable")
    quarantine = AsyncMock()
    monkeypatch.setattr(outbox, "claim_next", AsyncMock(return_value=row))
    monkeypatch.setattr(outbox, "mark_quarantined", quarantine)
    monkeypatch.setattr(outbox, "mark_failed", AsyncMock())
    monkeypatch.setattr(outbox, "mark_processed", AsyncMock())
    monkeypatch.setattr(outbox, "quarantine_exhausted", AsyncMock(return_value=0))

    assert await outbox.drain_once(pool, redis, AsyncMock()) is True

    quarantine.assert_awaited_once_with(
        pool,
        41,
        failure_class="attempts_exhausted",
        failure_code="delivery_attempts_exhausted",
        error="redis unavailable",
    )


@pytest.mark.asyncio
async def test_abandoned_final_attempt_is_terminalized_after_claim_timeout():
    pool = AsyncMock()
    pool.execute.return_value = "UPDATE 1"

    assert await outbox.quarantine_exhausted(pool) == 1

    sql = pool.execute.await_args.args[0]
    assert "attempts >= $1" in sql
    assert "failure_class = 'attempts_exhausted'" in sql
    assert "failure_code = 'delivery_attempts_exhausted'" in sql
    assert pool.execute.await_args.args[1:] == (
        outbox.MAX_DELIVERY_ATTEMPTS,
        outbox.CLAIM_TIMEOUT_SECONDS,
    )


@pytest.mark.asyncio
async def test_cleanup_uses_separate_bounded_skip_locked_horizons():
    pool = RecordingCleanupPool()

    assert await outbox.cleanup_once(pool) == {
        "processed": 2,
        "quarantined": 2,
        "receipts": 2,
    }

    assert len(pool.conn.calls) == 3
    processed, quarantined, receipts = pool.conn.calls
    for sql, args in pool.conn.calls:
        assert "FOR UPDATE" in sql
        assert "SKIP LOCKED" in sql
        assert "LIMIT $1" in sql
        assert args[0] == outbox.CLEANUP_BATCH_SIZE
    assert "ORDER BY processed_at, id" in processed[0]
    assert processed[1][1] == outbox.PROCESSED_RETENTION_SECONDS
    assert "ORDER BY quarantined_at, id" in quarantined[0]
    assert quarantined[1][1] == outbox.QUARANTINED_RETENTION_SECONDS
    assert "FROM config_exposure_receipts AS receipt" in receipts[0]
    assert "NOT EXISTS" in receipts[0]
    assert "outbox.kind = 'exposure'" in receipts[0]
    assert receipts[1][1] == outbox.EXPOSURE_RECEIPT_RETENTION_SECONDS


@pytest.mark.asyncio
async def test_worker_invokes_cleanup_before_delivery(monkeypatch):
    cleanup = AsyncMock(
        return_value={"processed": 0, "quarantined": 0, "receipts": 0}
    )
    drain = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(outbox, "cleanup_once", cleanup)
    monkeypatch.setattr(outbox, "drain_once", drain)

    with pytest.raises(asyncio.CancelledError):
        await outbox.run_worker(object(), object(), object())

    cleanup.assert_awaited_once()
    drain.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_continues_full_cleanup_batches_without_waiting(monkeypatch):
    cleanup = AsyncMock(
        side_effect=[
            {
                "processed": outbox.CLEANUP_BATCH_SIZE,
                "quarantined": 0,
                "receipts": 0,
            },
            asyncio.CancelledError,
        ]
    )
    drain = AsyncMock(return_value=False)
    sleep = AsyncMock()
    monkeypatch.setattr(outbox, "cleanup_once", cleanup)
    monkeypatch.setattr(outbox, "drain_once", drain)
    monkeypatch.setattr(outbox.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await outbox.run_worker(object(), object(), object())

    assert cleanup.await_count == 2
    drain.assert_awaited_once()
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_outbox_metrics_expose_lag_attempts_and_quarantine():
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "pending_count": 4,
        "processed_count": 12,
        "quarantined_count": 1,
        "estimated_receipt_count": 350,
        "max_pending_attempts": 3,
        "oldest_pending_age_seconds": 45.5,
        "oldest_processed_age_seconds": 3600.0,
        "oldest_quarantined_age_seconds": 12.25,
        "oldest_receipt_age_seconds": 7200.0,
    }

    metrics = await outbox.metrics_snapshot(conn)

    assert metrics == {
        "pending_count": 4,
        "processed_count": 12,
        "quarantined_count": 1,
        "estimated_receipt_count": 350,
        "max_pending_attempts": 3,
        "oldest_pending_age_seconds": 45.5,
        "oldest_processed_age_seconds": 3600.0,
        "oldest_quarantined_age_seconds": 12.25,
        "oldest_receipt_age_seconds": 7200.0,
    }
    sql = conn.fetchrow.await_args.args[0]
    assert "FROM config_outbox" in sql
    assert "FROM config_exposure_receipts" in sql
    assert "pg_stat_user_tables" in sql
    assert "quarantined_at IS NULL" in sql


@pytest.mark.parametrize(
    ("metrics_update", "reason"),
    [
        (
            {
                "oldest_pending_age_seconds": (
                    outbox.READINESS_MAX_PENDING_AGE_SECONDS + 1
                )
            },
            "oldest_pending_age_exceeded",
        ),
        (
            {"quarantined_count": outbox.READINESS_MAX_QUARANTINED_ROWS + 1},
            "quarantined_rows_exceeded",
        ),
        (
            {
                "oldest_processed_age_seconds": (
                    outbox.PROCESSED_RETENTION_SECONDS
                    + outbox.CLEANUP_READINESS_GRACE_SECONDS
                    + 1
                )
            },
            "processed_cleanup_overdue",
        ),
        (
            {
                "oldest_quarantined_age_seconds": (
                    outbox.QUARANTINED_RETENTION_SECONDS
                    + outbox.CLEANUP_READINESS_GRACE_SECONDS
                    + 1
                )
            },
            "quarantined_cleanup_overdue",
        ),
        (
            {
                "oldest_receipt_age_seconds": (
                    outbox.EXPOSURE_RECEIPT_RETENTION_SECONDS
                    + outbox.CLEANUP_READINESS_GRACE_SECONDS
                    + 1
                )
            },
            "receipt_cleanup_overdue",
        ),
    ],
)
def test_outbox_readiness_degrades_past_delivery_thresholds(
    metrics_update,
    reason,
):
    metrics = {**outbox.empty_metrics(), **metrics_update}

    readiness = outbox.readiness_snapshot(metrics)

    assert readiness["status"] == "degraded"
    assert readiness["degraded_reasons"] == [reason]


def test_exposure_receipts_outlive_clickhouse_event_retention():
    assert (
        outbox.EXPOSURE_RECEIPT_RETENTION_SECONDS
        > outbox.CLICKHOUSE_EVENT_RETENTION_MAX_SECONDS
    )
