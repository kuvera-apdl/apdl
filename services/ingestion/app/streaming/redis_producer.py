"""Atomic Redis Streams publisher for canonical event batches."""

import json

STREAM_MAXLEN = 1000000


async def publish_batch(redis, stream_key: str, events: list[dict]) -> list[str]:
    """Publish a complete batch in one Redis transaction.

    A connection failure before or after ``EXEC`` is intentionally reported as
    ambiguous failure. SDKs retry the same stable ``message_id`` values and the
    ClickHouse table deduplicates those retries.
    """
    pipeline = redis.pipeline(transaction=True)
    for event in events:
        event_json = json.dumps(
            event,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        pipeline.xadd(
            stream_key,
            {"event_json": event_json},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
    results = await pipeline.execute(raise_on_error=True)
    if len(results) != len(events):
        raise RuntimeError("Redis transaction returned an incomplete result set")
    return [str(result) for result in results]
