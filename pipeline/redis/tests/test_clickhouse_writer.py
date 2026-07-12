import asyncio
import json

import clickhouse_writer as writer_module
import pytest
from clickhouse_driver.errors import ServerException, TypeMismatchError
from clickhouse_writer import BufferedEvent, ClickHouseWriter


class FakeRedis:
    def __init__(self):
        self.acks: list[tuple[str, str, tuple[str, ...]]] = []
        self.read_calls = 0
        self.group_creates: list[dict] = []
        self.claim_calls: list[dict] = []
        self.claim_responses: list[list] = []
        self.read_args: list[dict] = []
        self.adds: list[tuple[str, dict, dict]] = []
        self.add_attempts: list[tuple[str, dict, dict]] = []
        self.operations: list[tuple[str, str, tuple[str, ...] | None]] = []
        self.fail_xadd = False

    async def xack(self, stream_key, group, *message_ids):
        self.operations.append(("xack", stream_key, message_ids))
        self.acks.append((stream_key, group, message_ids))
        return len(message_ids)

    async def xreadgroup(self, **kwargs):
        self.read_calls += 1
        self.read_args.append(kwargs)
        return []

    async def xadd(self, stream_key, fields, **kwargs):
        attempt = (stream_key, fields, kwargs)
        self.add_attempts.append(attempt)
        if self.fail_xadd:
            raise ConnectionError("redis unavailable")
        self.operations.append(("xadd", stream_key, None))
        self.adds.append(attempt)
        return "1-0"

    async def xgroup_create(self, **kwargs):
        self.group_creates.append(kwargs)
        return True

    async def xautoclaim(self, **kwargs):
        self.claim_calls.append(kwargs)
        if self.claim_responses:
            return self.claim_responses.pop(0)
        return ["0-0", [], []]


class FakeClickHouse:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.inserts: list[list[dict]] = []

    def execute(self, _query, rows, **_kwargs):
        self.inserts.append(rows)
        if self.fail:
            raise RuntimeError("clickhouse unavailable")


def make_writer(
    monkeypatch,
    *,
    redis_client=None,
    ch_client=None,
    buffer_size=10,
    **writer_kwargs,
):
    redis_client = redis_client or FakeRedis()
    ch_client = ch_client or FakeClickHouse()
    monkeypatch.setattr(
        writer_module.redis, "from_url", lambda *_args, **_kwargs: redis_client
    )
    monkeypatch.setattr(
        writer_module.ClickHouseClient,
        "from_url",
        lambda *_args, **_kwargs: ch_client,
    )
    writer = ClickHouseWriter(
        "redis://test",
        "clickhouse://test",
        buffer_size=buffer_size,
        **writer_kwargs,
    )
    return writer, redis_client, ch_client


def buffered_event(message_id="1-0"):
    return BufferedEvent(
        stream_key="events:raw:demo",
        message_id=message_id,
        row={"project_id": "demo"},
    )


def stream_message(message_id="1-0"):
    return (
        "events:raw:demo",
        [(message_id, {"event_json": json.dumps({"event": "signup"})})],
    )


def test_acks_only_after_clickhouse_insert_succeeds(monkeypatch):
    async def scenario():
        ch_client = FakeClickHouse(fail=True)
        writer, redis_client, _ = make_writer(monkeypatch, ch_client=ch_client)
        writer.buffer.append(buffered_event())

        assert await writer._flush() is False
        assert redis_client.acks == []
        assert len(writer.buffer) == 1

        ch_client.fail = False
        assert await writer._flush() is True
        assert writer.buffer == []
        assert redis_client.acks == [("events:raw:demo", "clickhouse-writer", ("1-0",))]

    asyncio.run(scenario())


def test_repeated_flush_failures_keep_events_and_apply_backpressure(monkeypatch):
    async def scenario():
        ch_client = FakeClickHouse(fail=True)
        writer, redis_client, _ = make_writer(
            monkeypatch,
            ch_client=ch_client,
            buffer_size=1,
        )
        writer.buffer.append(buffered_event())

        for _ in range(8):
            assert await writer._flush() is False

        assert writer.buffer == [buffered_event()]
        assert redis_client.acks == []
        assert writer._delivery_is_backpressured() is True
        assert writer.stats["flushed"] == 0

    asyncio.run(scenario())


def test_durable_rows_are_not_reinserted_when_redis_ack_retries(monkeypatch):
    class FlakyAckRedis(FakeRedis):
        def __init__(self):
            super().__init__()
            self.fail = True

        async def xack(self, stream_key, group, *message_ids):
            if self.fail:
                raise ConnectionError("redis unavailable")
            return await super().xack(stream_key, group, *message_ids)

    async def scenario():
        redis_client = FlakyAckRedis()
        writer, _, ch_client = make_writer(monkeypatch, redis_client=redis_client)
        writer.buffer.append(buffered_event())

        assert await writer._flush() is False
        assert len(ch_client.inserts) == 1
        assert writer.buffer == []
        assert writer._durable_pending_ack == {"events:raw:demo": ["1-0"]}

        redis_client.fail = False
        assert await writer._flush() is True
        assert len(ch_client.inserts) == 1
        assert redis_client.acks == [("events:raw:demo", "clickhouse-writer", ("1-0",))]

    asyncio.run(scenario())


def test_reclaims_and_flushes_stale_messages_from_prior_consumers(monkeypatch):
    async def scenario():
        redis_client = FakeRedis()
        redis_client.claim_responses = [
            ["0-0", stream_message()[1], []],
        ]
        writer, _, ch_client = make_writer(monkeypatch, redis_client=redis_client)
        writer.running = True

        await writer._process_pending(["demo"])

        assert len(ch_client.inserts) == 1
        assert redis_client.acks == [("events:raw:demo", "clickhouse-writer", ("1-0",))]
        assert redis_client.claim_calls == [
            {
                "name": "events:raw:demo",
                "groupname": "clickhouse-writer",
                "consumername": writer.consumer_name,
                "min_idle_time": 60_000,
                "start_id": "0-0",
                "count": 10,
            }
        ]
        assert redis_client.read_calls == 0

    asyncio.run(scenario())


def test_claim_cursor_scans_past_ineligible_pending_entries(monkeypatch):
    async def scenario():
        redis_client = FakeRedis()
        redis_client.claim_responses = [
            ["5-0", [], []],
            ["0-0", [], []],
        ]
        writer, _, _ = make_writer(monkeypatch, redis_client=redis_client)
        writer.running = True

        await writer._process_pending(["demo"])

        assert [call["start_id"] for call in redis_client.claim_calls] == [
            "0-0",
            "5-0",
        ]

    asyncio.run(scenario())


def test_new_consumer_groups_start_before_existing_backlog(monkeypatch):
    async def scenario():
        writer, redis_client, _ = make_writer(monkeypatch)

        await writer._ensure_consumer_groups(["demo"])
        assert redis_client.group_creates[-1] == {
            "name": "events:raw:demo",
            "groupname": "clickhouse-writer",
            "id": "0-0",
            "mkstream": True,
        }

        redis_client.group_creates.clear()
        assert await writer._get_stream_keys(["demo"]) == ["events:raw:demo"]
        assert redis_client.group_creates[-1]["id"] == "0-0"

    asyncio.run(scenario())


def test_project_authority_is_derived_from_validated_stream_key(monkeypatch):
    async def scenario():
        writer, redis_client, ch_client = make_writer(monkeypatch, buffer_size=1)
        event_json = {
            "event": "signup",
            "project_id": "demo",
        }
        results = [
            (
                "events:raw:demo",
                [
                    (
                        "1-0",
                        {
                            "project_id": "demo",
                            "event_json": json.dumps(event_json),
                        },
                    )
                ],
            )
        ]

        assert await writer._process_messages(results) == 1
        assert ch_client.inserts[0][0]["project_id"] == "demo"
        assert redis_client.acks == [("events:raw:demo", "clickhouse-writer", ("1-0",))]

    asyncio.run(scenario())


def test_conflicting_project_assertions_are_rejected_without_ack(monkeypatch):
    async def scenario():
        writer, redis_client, ch_client = make_writer(monkeypatch)
        results = [
            (
                "events:raw:demo",
                [
                    (
                        "1-0",
                        {
                            "project_id": "victim",
                            "event_json": json.dumps(
                                {"event": "signup", "project_id": "demo"}
                            ),
                        },
                    ),
                    (
                        "2-0",
                        {
                            "event_json": json.dumps(
                                {"event": "signup", "project_id": "victim"}
                            )
                        },
                    ),
                ],
            )
        ]

        assert await writer._process_messages(results) == 0
        assert writer.buffer == []
        assert ch_client.inserts == []
        assert redis_client.acks == []

    asyncio.run(scenario())


def test_invalid_stream_project_is_rejected(monkeypatch):
    writer, _, _ = make_writer(monkeypatch)

    for stream_key in (
        "events:raw:",
        "events:raw:has-hyphen",
        "events:raw:demo:other",
        "other:raw:demo",
    ):
        try:
            writer._project_id_from_stream(stream_key)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid stream key {stream_key!r}")


def test_type_only_nullable_legacy_event_projects_to_canonical_row(monkeypatch):
    writer, _, _ = make_writer(monkeypatch)

    row = writer._parse_event(
        {
            "event_json": json.dumps(
                {
                    "type": "identify",
                    "userId": "user-1",
                    "anonymousId": "anon-1",
                    "properties": None,
                    "context": None,
                }
            )
        },
        "demo",
    )

    assert row["event_name"] == "identify"
    assert row["user_id"] == "user-1"
    assert row["anonymous_id"] == "anon-1"
    assert row["properties"] == "{}"
    assert row["device_type"] == ""


def test_invalid_canonical_row_is_dlqd_before_source_ack(monkeypatch):
    async def scenario():
        writer, redis_client, ch_client = make_writer(monkeypatch)
        results = [
            (
                "events:raw:demo",
                [("1-0", {"event_json": json.dumps({"event": ["bad"]})})],
            )
        ]

        assert await writer._process_messages(results) == 0
        assert ch_client.inserts == []
        assert redis_client.acks == []
        assert len(redis_client.adds) == 1
        dlq_stream, fields, options = redis_client.adds[0]
        assert dlq_stream == "events:dlq:demo"
        assert options == {"maxlen": 10_000, "approximate": False}
        assert fields["source_stream"] == "events:raw:demo"
        assert fields["source_message_id"] == "1-0"
        assert fields["reason_code"] == "invalid_event_schema"
        assert fields["error_type"] == "TypeError"
        assert "event_json" not in fields

        assert await writer._flush() is True
        assert redis_client.acks == [("events:raw:demo", "clickhouse-writer", ("1-0",))]
        assert [operation[0] for operation in redis_client.operations] == [
            "xadd",
            "xack",
        ]

    asyncio.run(scenario())


def test_dlq_failure_leaves_reject_pending_while_valid_event_flushes(monkeypatch):
    async def scenario():
        redis_client = FakeRedis()
        redis_client.fail_xadd = True
        writer, _, ch_client = make_writer(
            monkeypatch,
            redis_client=redis_client,
            buffer_size=1,
        )
        results = [
            (
                "events:raw:demo",
                [
                    ("1-0", {"event_json": json.dumps({"event": ["bad"]})}),
                    ("2-0", {"event_json": json.dumps({"event": "signup"})}),
                ],
            )
        ]

        assert await writer._process_messages(results) == 1
        assert [row["event_name"] for row in ch_client.inserts[0]] == ["signup"]
        assert redis_client.acks == [("events:raw:demo", "clickhouse-writer", ("2-0",))]
        assert len(redis_client.add_attempts) == 1
        assert redis_client.adds == []

    asyncio.run(scenario())


def test_insert_poison_isolated_without_blocking_valid_row(monkeypatch):
    class RowRejectingClickHouse(FakeClickHouse):
        def execute(self, _query, rows, **_kwargs):
            self.inserts.append(rows)
            if any(row["event_name"] == "poison" for row in rows):
                raise TypeMismatchError("local row serialization failed")

    async def scenario():
        ch_client = RowRejectingClickHouse()
        writer, redis_client, _ = make_writer(
            monkeypatch,
            ch_client=ch_client,
            buffer_size=2,
        )
        results = [
            (
                "events:raw:demo",
                [("1-0", {"event_json": json.dumps({"event": "signup"})})],
            ),
            (
                "events:raw:other",
                [("2-0", {"event_json": json.dumps({"event": "poison"})})],
            ),
        ]

        assert await writer._process_messages(results) == 2
        assert writer.buffer == []
        assert writer.stats["flushed"] == 1
        assert len(ch_client.inserts) == 3
        assert [row["event_name"] for row in ch_client.inserts[1]] == ["signup"]
        assert redis_client.adds[0][0] == "events:dlq:other"
        assert redis_client.adds[0][1]["reason_code"] == "clickhouse_row_rejected"
        assert {
            (stream, message_id)
            for stream, _group, message_ids in redis_client.acks
            for message_id in message_ids
        } == {
            ("events:raw:demo", "1-0"),
            ("events:raw:other", "2-0"),
        }

    asyncio.run(scenario())


def test_server_schema_error_is_not_bisected_or_dead_lettered(monkeypatch):
    class SchemaErrorClickHouse(FakeClickHouse):
        def execute(self, _query, rows, **_kwargs):
            self.inserts.append(rows)
            raise ServerException("server schema mismatch", code=53)

    async def scenario():
        ch_client = SchemaErrorClickHouse()
        writer, redis_client, _ = make_writer(monkeypatch, ch_client=ch_client)
        writer.buffer.extend([buffered_event("1-0"), buffered_event("2-0")])

        assert await writer._flush() is False
        assert len(ch_client.inserts) == 1
        assert [event.message_id for event in writer.buffer] == ["1-0", "2-0"]
        assert redis_client.add_attempts == []
        assert redis_client.acks == []

    asyncio.run(scenario())


def test_transient_outage_is_not_bisected_or_dead_lettered(monkeypatch):
    async def scenario():
        ch_client = FakeClickHouse(fail=True)
        writer, redis_client, _ = make_writer(monkeypatch, ch_client=ch_client)
        writer.buffer.extend([buffered_event("1-0"), buffered_event("2-0")])

        assert await writer._flush() is False
        assert len(ch_client.inserts) == 1
        assert [event.message_id for event in writer.buffer] == ["1-0", "2-0"]
        assert redis_client.add_attempts == []

    asyncio.run(scenario())


def test_multistream_overdelivery_never_exceeds_global_buffer(monkeypatch):
    async def scenario():
        ch_client = FakeClickHouse(fail=True)
        writer, redis_client, _ = make_writer(
            monkeypatch,
            ch_client=ch_client,
            buffer_size=1,
        )
        results = [
            stream_message("1-0"),
            (
                "events:raw:other",
                [("2-0", {"event_json": json.dumps({"event": "purchase"})})],
            ),
        ]

        assert await writer._process_messages(results) == 1
        assert len(writer.buffer) == writer.buffer_size == 1
        assert writer.buffer[0].message_id == "1-0"
        assert len(ch_client.inserts) == 1
        assert redis_client.acks == []

    asyncio.run(scenario())


def test_new_and_pending_streams_rotate_fairly_with_remaining_capacity(monkeypatch):
    async def scenario():
        writer, redis_client, _ = make_writer(monkeypatch, buffer_size=2)
        keys = ["events:raw:zeta", "events:raw:alpha"]

        assert writer._next_stream_key(keys, pending=False) == "events:raw:alpha"
        assert writer._next_stream_key(keys, pending=False) == "events:raw:zeta"
        assert writer._next_stream_key(keys, pending=False) == "events:raw:alpha"

        writer.buffer.append(buffered_event())
        writer.running = True
        await writer._process_pending(["zeta", "alpha"])
        await writer._process_pending(["zeta", "alpha"])

        assert [call["name"] for call in redis_client.claim_calls] == [
            "events:raw:alpha",
            "events:raw:zeta",
            "events:raw:zeta",
            "events:raw:alpha",
        ]
        assert {call["count"] for call in redis_client.claim_calls} == {1}

    asyncio.run(scenario())


def test_consume_reads_one_stream_per_call_in_round_robin_order(monkeypatch):
    class StoppingRedis(FakeRedis):
        writer = None

        async def xreadgroup(self, **kwargs):
            await super().xreadgroup(**kwargs)
            if self.read_calls == 2:
                self.writer.running = False
            return []

    async def scenario():
        redis_client = StoppingRedis()
        writer, _, _ = make_writer(monkeypatch, redis_client=redis_client)
        redis_client.writer = writer
        writer.running = True

        await writer._consume_loop(["zeta", "alpha"])

        assert [call["streams"] for call in redis_client.read_args] == [
            {"events:raw:alpha": ">"},
            {"events:raw:zeta": ">"},
        ]
        assert {call["count"] for call in redis_client.read_args} == {
            writer.buffer_size
        }

    asyncio.run(scenario())


def test_periodic_flush_waits_for_shared_retry_deadline(monkeypatch):
    async def scenario():
        clock = [100.0]
        monkeypatch.setattr(writer_module.time, "monotonic", lambda: clock[0])
        ch_client = FakeClickHouse(fail=True)
        writer, _, _ = make_writer(monkeypatch, ch_client=ch_client)
        writer.buffer.append(buffered_event())

        assert await writer._flush() is False
        assert writer._next_flush_retry_at == pytest.approx(101.0)
        assert len(ch_client.inserts) == 1

        async def one_tick(_delay):
            writer.running = False

        monkeypatch.setattr(writer_module.asyncio, "sleep", one_tick)
        clock[0] = 100.5
        writer.running = True
        await writer._flush_loop()
        assert len(ch_client.inserts) == 1

        clock[0] = 101.0
        writer.running = True
        await writer._flush_loop()
        assert len(ch_client.inserts) == 2

    asyncio.run(scenario())
