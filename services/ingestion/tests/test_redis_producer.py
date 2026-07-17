import logging
from unittest.mock import AsyncMock

import pytest

from app.streaming import redis_producer
from app.streaming.redis_producer import (
    EVENT_STREAM_ALERT_ENTRIES,
    EVENT_STREAM_MAX_ENTRIES,
    StreamOverloaded,
    publish_batch,
)


@pytest.fixture(autouse=True)
def clear_alert_log_state():
    redis_producer._last_alert_log.clear()


@pytest.mark.asyncio
async def test_bounded_admission_publishes_complete_batch_atomically():
    redis = AsyncMock()
    redis.eval.return_value = [1, 12, b"1-0", b"2-0"]

    result = await publish_batch(
        redis,
        "events:raw:demo",
        [{"message_id": "one"}, {"message_id": "two"}],
    )

    assert result == ["1-0", "2-0"]
    args = redis.eval.await_args.args
    assert args[2] == "events:raw:demo"
    assert args[3] == 2
    assert args[4] == EVENT_STREAM_MAX_ENTRIES
    assert "MAXLEN" not in args[0]
    assert '"message_id":"one"' in args[5]
    assert '"message_id":"two"' in args[6]


@pytest.mark.asyncio
async def test_projected_depth_at_capacity_is_accepted():
    redis = AsyncMock()
    redis.eval.return_value = [1, EVENT_STREAM_MAX_ENTRIES, "1-0"]

    assert await publish_batch(redis, "events:raw:demo", [{}]) == ["1-0"]


@pytest.mark.asyncio
async def test_batch_over_capacity_is_rejected_without_partial_acceptance():
    redis = AsyncMock()
    redis.eval.return_value = [0, EVENT_STREAM_MAX_ENTRIES]

    with pytest.raises(StreamOverloaded) as exc_info:
        await publish_batch(redis, "events:raw:demo", [{}, {}])

    assert exc_info.value.current_entries == EVENT_STREAM_MAX_ENTRIES


@pytest.mark.asyncio
async def test_incomplete_admission_result_is_an_ambiguous_failure():
    redis = AsyncMock()
    redis.eval.return_value = [1, 10, "1-0"]

    with pytest.raises(RuntimeError, match="incomplete"):
        await publish_batch(redis, "events:raw:demo", [{}, {}])


@pytest.mark.asyncio
async def test_pressure_warning_contains_metadata_but_not_event_payload(caplog):
    redis = AsyncMock()
    redis.eval.return_value = [1, EVENT_STREAM_ALERT_ENTRIES, "1-0"]

    with caplog.at_level(
        logging.WARNING,
        logger="app.streaming.redis_producer",
    ):
        await publish_batch(
            redis,
            "events:raw:demo",
            [{"message_id": "never-log-this-payload"}],
        )

    message = caplog.messages[-1]
    assert "event_stream_pressure" in message
    assert str(EVENT_STREAM_ALERT_ENTRIES) in message
    assert "never-log-this-payload" not in message


@pytest.mark.asyncio
async def test_pressure_alert_is_rate_limited_per_stream(caplog):
    redis = AsyncMock()
    redis.eval.return_value = [1, EVENT_STREAM_ALERT_ENTRIES, "1-0"]

    with caplog.at_level(
        logging.WARNING,
        logger="app.streaming.redis_producer",
    ):
        await publish_batch(redis, "events:raw:demo", [{}])
        await publish_batch(redis, "events:raw:demo", [{}])

    pressure_logs = [
        message for message in caplog.messages if "event_stream_pressure" in message
    ]
    assert len(pressure_logs) == 1
