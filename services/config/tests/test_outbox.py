"""Durable Config outbox delivery and retry tests."""

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


def exposure_row(*, attempts: int) -> dict:
    return {
        "id": 41,
        "project_id": "apdl",
        "kind": "exposure",
        "attempts": attempts,
        "payload": {
            "stream_key": "events:raw:apdl",
            "event": {"message_id": "srv-1", "event": "$feature_flag_exposure"},
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
async def test_failed_head_blocks_newer_rows_only_in_its_project_kind_domain():
    pool = RecordingClaimPool()

    assert await outbox.claim_next(pool) is None

    sql = pool.conn.sql
    assert "NOT EXISTS" in sql
    assert "earlier.project_id = pending.project_id" in sql
    assert "earlier.kind = pending.kind" in sql
    assert "earlier.processed_at IS NULL" in sql
    assert "earlier.id < pending.id" in sql
    # Failed head N remains unprocessed while its backoff delays availability,
    # so the anti-join excludes N+1. Other projects/kinds remain independent.
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
            "data": {
                "action": "flag_updated",
                "key": "checkout",
                "version": 7,
            },
        },
    }

    await outbox.deliver(row, object(), broadcaster)

    invalidate.assert_awaited_once()
    project_id, event_type, raw = broadcaster.broadcast.await_args.args
    assert project_id == "apdl"
    assert event_type == "flag_update"
    assert json.loads(raw)["version"] == 7


@pytest.mark.asyncio
async def test_no_due_outbox_row_is_idle(monkeypatch):
    monkeypatch.setattr(outbox, "claim_next", AsyncMock(return_value=None))

    assert await outbox.drain_once(object(), object(), object()) is False


@pytest.mark.asyncio
async def test_unknown_outbox_kind_fails_closed():
    with pytest.raises(ValueError, match="Unsupported"):
        await outbox.deliver(
            {"project_id": "apdl", "kind": "unknown", "payload": {}},
            object(),
            object(),
        )
