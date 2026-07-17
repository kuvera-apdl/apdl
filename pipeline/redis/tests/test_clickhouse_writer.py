import asyncio
import json
import logging
from pathlib import Path

import clickhouse_writer as writer_module
import pytest
from clickhouse_driver.errors import ServerException, TypeMismatchError
from clickhouse_writer import BufferedEvent, ClickHouseWriter

CONTRACT_FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "events" / "canonical.json"
)


class FakePipeline:
    def __init__(self, redis_client, *, transaction):
        self.redis_client = redis_client
        self.transaction = transaction
        self.commands = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def xack(self, stream_key, group, *message_ids):
        self.commands.append(("xack", stream_key, group, message_ids))
        return self

    def xdel(self, stream_key, *message_ids):
        self.commands.append(("xdel", stream_key, message_ids))
        return self

    async def execute(self):
        self.redis_client.transaction_attempts.append(tuple(self.commands))
        if self.redis_client.fail_transaction:
            raise ConnectionError("redis unavailable before EXEC")

        results = []
        for command in self.commands:
            if command[0] == "xack":
                _, stream_key, group, message_ids = command
                self.redis_client.operations.append(("xack", stream_key, message_ids))
                self.redis_client.acks.append((stream_key, group, message_ids))
                results.append(len(message_ids))
            else:
                _, stream_key, message_ids = command
                self.redis_client.operations.append(("xdel", stream_key, message_ids))
                self.redis_client.deletes.append((stream_key, message_ids))
                results.append(len(message_ids))

        if self.redis_client.fail_transaction_after_commit:
            self.redis_client.fail_transaction_after_commit = False
            raise ConnectionError("redis unavailable after EXEC")
        return results


class FakeRedis:
    def __init__(self):
        self.acks: list[tuple[str, str, tuple[str, ...]]] = []
        self.deletes: list[tuple[str, tuple[str, ...]]] = []
        self.read_calls = 0
        self.group_creates: list[dict] = []
        self.claim_calls: list[dict] = []
        self.claim_responses: list[list] = []
        self.read_args: list[dict] = []
        self.stream_lengths: dict[str, int] = {}
        self.stream_groups: dict[str, list[dict]] = {}
        self.pending_summaries: dict[str, dict] = {}
        self.trim_calls: list[dict] = []
        self.xlen_calls: list[str] = []
        self.xinfo_group_calls: list[str] = []
        self.memory_info = {"used_memory": 0, "maxmemory": 0}
        self.adds: list[tuple[str, dict, dict]] = []
        self.add_attempts: list[tuple[str, dict, dict]] = []
        self.operations: list[tuple[str, str, tuple[str, ...] | None]] = []
        self.fail_xadd = False
        self.fail_transaction = False
        self.fail_transaction_after_commit = False
        self.pipeline_transactions: list[bool] = []
        self.transaction_attempts: list[tuple] = []

    def pipeline(self, *, transaction):
        self.pipeline_transactions.append(transaction)
        return FakePipeline(self, transaction=transaction)

    async def xreadgroup(self, **kwargs):
        self.read_calls += 1
        self.read_args.append(kwargs)
        return []

    async def xlen(self, stream_key):
        self.xlen_calls.append(stream_key)
        return self.stream_lengths.get(stream_key, 0)

    async def xinfo_groups(self, stream_key):
        self.xinfo_group_calls.append(stream_key)
        return self.stream_groups.get(
            stream_key,
            [
                {
                    "name": "clickhouse-writer",
                    "pending": 0,
                    "lag": 0,
                    "last-delivered-id": "0-0",
                }
            ],
        )

    async def xpending(self, stream_key, _group):
        return self.pending_summaries.get(
            stream_key,
            {"pending": 0, "min": None, "max": None, "consumers": []},
        )

    async def xtrim(self, stream_key, **kwargs):
        self.trim_calls.append({"name": stream_key, **kwargs})
        return self.stream_lengths.get(stream_key, 0)

    async def info(self, section):
        assert section == "memory"
        return self.memory_info

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


def canonical_event(event="signup", **overrides):
    value = {
        "event": event,
        "type": "track",
        "anonymous_id": "anon-test",
        "timestamp": "2026-07-13T12:00:00.000Z",
        "context": {},
        "message_id": f"message-{event}",
    }
    value.update(overrides)
    return value


def stream_message(message_id="1-0"):
    return (
        "events:raw:demo",
        [(message_id, {"event_json": json.dumps(canonical_event())})],
    )


def finalize_commands(message_ids=("1-0",), stream_key="events:raw:demo"):
    return (
        ("xack", stream_key, "clickhouse-writer", message_ids),
        ("xdel", stream_key, message_ids),
    )


def test_ack_and_delete_only_after_clickhouse_insert_succeeds(monkeypatch):
    async def scenario():
        ch_client = FakeClickHouse(fail=True)
        writer, redis_client, _ = make_writer(monkeypatch, ch_client=ch_client)
        writer.buffer.append(buffered_event())

        assert await writer._flush() is False
        assert redis_client.acks == []
        assert redis_client.deletes == []
        assert redis_client.transaction_attempts == []
        assert len(writer.buffer) == 1

        ch_client.fail = False
        assert await writer._flush() is True
        assert writer.buffer == []
        assert redis_client.acks == [("events:raw:demo", "clickhouse-writer", ("1-0",))]
        assert redis_client.deletes == [("events:raw:demo", ("1-0",))]
        assert redis_client.pipeline_transactions == [True]
        assert redis_client.transaction_attempts == [finalize_commands()]

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
            self.fail_transaction = True

    async def scenario():
        redis_client = FlakyAckRedis()
        writer, _, ch_client = make_writer(monkeypatch, redis_client=redis_client)
        writer.buffer.append(buffered_event())

        assert await writer._flush() is False
        assert len(ch_client.inserts) == 1
        assert writer.buffer == []
        assert writer._durable_pending_ack == {"events:raw:demo": ["1-0"]}
        assert redis_client.acks == []
        assert redis_client.deletes == []
        assert redis_client.transaction_attempts == [finalize_commands()]

        redis_client.fail_transaction = False
        assert await writer._flush() is True
        assert len(ch_client.inserts) == 1
        assert redis_client.acks == [("events:raw:demo", "clickhouse-writer", ("1-0",))]
        assert redis_client.deletes == [("events:raw:demo", ("1-0",))]
        assert redis_client.transaction_attempts == [
            finalize_commands(),
            finalize_commands(),
        ]
        assert writer._durable_pending_ack == {}

    asyncio.run(scenario())


def test_finalize_retry_is_idempotent_after_unknown_exec_outcome(monkeypatch):
    async def scenario():
        redis_client = FakeRedis()
        redis_client.fail_transaction_after_commit = True
        writer, _, ch_client = make_writer(monkeypatch, redis_client=redis_client)
        writer.buffer.append(buffered_event())

        assert await writer._flush() is False
        assert len(ch_client.inserts) == 1
        assert writer._durable_pending_ack == {"events:raw:demo": ["1-0"]}

        assert await writer._flush() is True
        assert len(ch_client.inserts) == 1
        assert redis_client.transaction_attempts == [
            finalize_commands(),
            finalize_commands(),
        ]
        assert redis_client.acks == [
            ("events:raw:demo", "clickhouse-writer", ("1-0",)),
            ("events:raw:demo", "clickhouse-writer", ("1-0",)),
        ]
        assert redis_client.deletes == [
            ("events:raw:demo", ("1-0",)),
            ("events:raw:demo", ("1-0",)),
        ]
        assert writer._durable_pending_ack == {}

    asyncio.run(scenario())


def test_crash_after_insert_replay_converges_on_one_storage_identity(monkeypatch):
    """A stable client message ID collapses an insert-before-ACK replay."""

    class IdempotentClickHouse(FakeClickHouse):
        def __init__(self):
            super().__init__()
            self.rows: dict[tuple[str, str], dict] = {}

        def execute(self, _query, rows, **_kwargs):
            self.inserts.append(rows)
            for row in rows:
                self.rows[(row["project_id"], row["message_id"])] = row

    class FailedFinalizeRedis(FakeRedis):
        def __init__(self):
            super().__init__()
            self.fail_transaction = True

    async def scenario():
        clickhouse = IdempotentClickHouse()
        payload = canonical_event(message_id="client-stable-id")
        delivery = [
            (
                "events:raw:demo",
                [("1-0", {"event_json": json.dumps(payload)})],
            )
        ]

        first, _, _ = make_writer(
            monkeypatch,
            redis_client=FailedFinalizeRedis(),
            ch_client=clickhouse,
            buffer_size=1,
        )
        assert await first._process_messages(delivery) == 1
        assert len(clickhouse.rows) == 1

        restarted, redis_client, _ = make_writer(
            monkeypatch,
            redis_client=FakeRedis(),
            ch_client=clickhouse,
            buffer_size=1,
        )
        assert await restarted._process_messages(delivery) == 1

        assert len(clickhouse.inserts) == 2
        assert list(clickhouse.rows) == [("demo", "client-stable-id")]
        assert redis_client.acks == [
            ("events:raw:demo", "clickhouse-writer", ("1-0",))
        ]

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
        assert redis_client.deletes == [("events:raw:demo", ("1-0",))]
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


def test_reports_deleted_pending_ids_from_xautoclaim(monkeypatch, caplog):
    async def scenario():
        redis_client = FakeRedis()
        redis_client.claim_responses = [
            ["0-0", [], ["7-0", "8-0"]],
        ]
        writer, _, _ = make_writer(monkeypatch, redis_client=redis_client)
        writer.running = True

        with caplog.at_level(logging.CRITICAL, logger=writer_module.__name__):
            await writer._process_pending(["demo"])

        assert writer.stats["lost_or_deleted_pending"] == 2
        record = next(
            record
            for record in caplog.records
            if record.levelno == logging.CRITICAL
            and "events:raw:demo" in record.getMessage()
        )
        assert "7-0, 8-0" in record.getMessage()
        assert record.event == "lost_or_deleted_pending"
        assert record.deleted_pending_count == 2

    asyncio.run(scenario())


def test_stream_pressure_logs_exact_values_at_a_bounded_interval(
    monkeypatch, caplog
):
    async def scenario():
        clock = [100.0]
        monkeypatch.setattr(writer_module.time, "monotonic", lambda: clock[0])
        redis_client = FakeRedis()
        redis_client.stream_lengths["events:raw:demo"] = 750_000
        redis_client.stream_groups["events:raw:demo"] = [
            {
                "name": "clickhouse-writer",
                "pending": 125,
                "lag": None,
            }
        ]
        writer, _, _ = make_writer(monkeypatch, redis_client=redis_client)

        with caplog.at_level(logging.INFO, logger=writer_module.__name__):
            await writer._log_due_stream_pressure(["events:raw:demo"])
            clock[0] = 129.9
            await writer._log_due_stream_pressure(["events:raw:demo"])
            clock[0] = 130.0
            await writer._log_due_stream_pressure(["events:raw:demo"])

        assert redis_client.xlen_calls == [
            "events:raw:demo",
            "events:raw:demo",
        ]
        assert redis_client.xinfo_group_calls == [
            "events:raw:demo",
            "events:raw:demo",
        ]
        pressure_records = [
            record
            for record in caplog.records
            if "event_stream_pressure" in record.getMessage()
        ]
        assert len(pressure_records) == 2
        assert {record.levelno for record in pressure_records} == {logging.WARNING}
        record = pressure_records[0]
        assert record.event == "event_stream_pressure"
        assert record.stream_key == "events:raw:demo"
        assert record.outstanding_entries == 750_000
        assert record.pending == 125
        assert record.lag is None
        assert record.lag_unknown is True
        assert record.alert_entries == 750_000
        assert record.max_entries == 1_000_000
        message = record.getMessage()
        assert "outstanding_entries=750000" in message
        assert "pending=125" in message
        assert "lag=None" in message
        assert "lag_unknown=true" in message
        assert "alert_entries=750000" in message
        assert "max_entries=1000000" in message

    asyncio.run(scenario())


def test_reconciles_only_acknowledged_pre_upgrade_history(monkeypatch):
    async def scenario():
        redis_client = FakeRedis()
        redis_client.stream_lengths = {
            "events:raw:clear": 500,
            "events:raw:pending": 500,
        }
        redis_client.stream_groups = {
            "events:raw:clear": [
                {
                    "name": "clickhouse-writer",
                    "pending": 0,
                    "lag": 20,
                    "last-delivered-id": "500-7",
                }
            ],
            "events:raw:pending": [
                {
                    "name": "clickhouse-writer",
                    "pending": 4,
                    "lag": 20,
                    "last-delivered-id": "500-7",
                }
            ],
        }
        redis_client.pending_summaries["events:raw:pending"] = {
            "pending": 4,
            "min": "300-2",
            "max": "490-0",
            "consumers": [],
        }
        writer, _, _ = make_writer(monkeypatch, redis_client=redis_client)

        await writer._reconcile_acknowledged_history(
            ["events:raw:clear", "events:raw:pending"]
        )

        assert redis_client.trim_calls == [
            {
                "name": "events:raw:clear",
                "minid": "500-8",
                "approximate": False,
            },
            {
                "name": "events:raw:pending",
                "minid": "300-2",
                "approximate": False,
            },
        ]

    asyncio.run(scenario())


def test_preflight_reconciliation_can_skip_stream_without_group(monkeypatch):
    async def scenario():
        redis_client = FakeRedis()
        redis_client.stream_groups["events:raw:new"] = []
        writer, _, _ = make_writer(monkeypatch, redis_client=redis_client)

        await writer._reconcile_acknowledged_history(
            ["events:raw:new"],
            require_group=False,
        )

        assert redis_client.trim_calls == []

    asyncio.run(scenario())


def test_shared_redis_memory_pressure_emits_warning(monkeypatch, caplog):
    async def scenario():
        redis_client = FakeRedis()
        redis_client.memory_info = {
            "used_memory": 400_000_000,
            "maxmemory": 500_000_000,
        }
        writer, _, _ = make_writer(monkeypatch, redis_client=redis_client)

        with caplog.at_level(logging.INFO, logger=writer_module.__name__):
            await writer._log_redis_memory_pressure()

        record = next(
            record
            for record in caplog.records
            if getattr(record, "event", None) == "redis_memory_pressure"
        )
        assert record.levelno == logging.WARNING
        assert record.used_memory_bytes == 400_000_000
        assert record.max_memory_bytes == 500_000_000
        assert record.utilization == 0.8
        assert record.alert_ratio == 0.75

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
        redis_client.stream_groups["events:raw:demo"] = []

        await writer._ensure_consumer_groups(["demo"])
        assert redis_client.group_creates[-1] == {
            "name": "events:raw:demo",
            "groupname": "clickhouse-writer",
            "id": "0-0",
            "mkstream": True,
        }

        redis_client.group_creates.clear()
        redis_client.stream_groups["events:raw:demo"] = []
        assert await writer._get_stream_keys(["demo"]) == ["events:raw:demo"]
        assert redis_client.group_creates[-1]["id"] == "0-0"

    asyncio.run(scenario())


def test_existing_consumer_group_avoids_group_creation_write(monkeypatch):
    async def scenario():
        writer, redis_client, _ = make_writer(monkeypatch)

        await writer._ensure_consumer_groups(["demo"])

        assert redis_client.group_creates == []
        assert redis_client.xinfo_group_calls == ["events:raw:demo"]

    asyncio.run(scenario())


def test_start_reconciles_existing_groups_before_group_creation(monkeypatch):
    async def scenario():
        writer, _, _ = make_writer(monkeypatch)
        calls = []

        async def discover_streams():
            calls.append(("discover",))
            return ["events:raw:demo"]

        async def reconcile(stream_keys, *, require_group=True):
            calls.append(("reconcile", tuple(stream_keys), require_group))

        async def ensure(project_ids):
            calls.append(("ensure", tuple(project_ids or [])))

        async def get_stream_keys(project_ids):
            calls.append(("get", tuple(project_ids or [])))
            return ["events:raw:demo"]

        async def no_op(*_args, **_kwargs):
            return None

        monkeypatch.setattr(writer, "_discover_streams", discover_streams)
        monkeypatch.setattr(writer, "_reconcile_acknowledged_history", reconcile)
        monkeypatch.setattr(writer, "_ensure_consumer_groups", ensure)
        monkeypatch.setattr(writer, "_get_stream_keys", get_stream_keys)
        monkeypatch.setattr(writer, "_log_due_stream_pressure", no_op)
        monkeypatch.setattr(writer, "_log_redis_memory_pressure", no_op)
        monkeypatch.setattr(writer, "_consume_loop", no_op)
        monkeypatch.setattr(writer, "_flush_loop", no_op)
        monkeypatch.setattr(writer, "_monitor_loop", no_op)

        await writer.start(["demo"])

        assert calls[:5] == [
            ("discover",),
            ("reconcile", ("events:raw:demo",), False),
            ("ensure", ("demo",)),
            ("get", ("demo",)),
            ("reconcile", ("events:raw:demo",), True),
        ]

    asyncio.run(scenario())


def test_project_authority_is_derived_from_validated_stream_key(monkeypatch):
    async def scenario():
        writer, redis_client, ch_client = make_writer(monkeypatch, buffer_size=1)
        event_json = canonical_event(project_id="demo")
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
                                canonical_event(project_id="demo")
                            ),
                        },
                    ),
                    (
                        "2-0",
                        {
                            "event_json": json.dumps(
                                canonical_event(project_id="victim")
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


def test_writer_uses_shared_canonical_contract_fixture(monkeypatch):
    writer, _, _ = make_writer(monkeypatch)
    fixture = json.loads(CONTRACT_FIXTURE.read_text())

    rows = []
    for event in fixture["valid"]:
        row = writer._parse_event({"event_json": json.dumps(event)}, "demo")
        assert row["message_id"] == event["message_id"]
        assert row["event_type"] == event["type"]
        rows.append(row)

    assert json.loads(rows[0]["context"])["browser"]["name"] == "Firefox"
    assert rows[0]["browser"] == "Firefox"
    assert json.loads(rows[1]["traits"]) == {"plan": "pro"}
    assert rows[2]["group_id"] == "account-7"

    for case in fixture["invalid"]:
        with pytest.raises((TypeError, ValueError)):
            writer._parse_event(
                {"event_json": json.dumps(case["event"])},
                "demo",
            )


def test_legacy_alias_event_is_rejected(monkeypatch):
    writer, _, _ = make_writer(monkeypatch)

    with pytest.raises(ValueError, match="unknown fields"):
        writer._parse_event(
            {
                "event_json": json.dumps({
                    "event": "identify",
                    "type": "identify",
                    "userId": "user-1",
                    "anonymousId": "anon-1",
                    "timestamp": "2026-07-13T12:00:00.000Z",
                    "context": {},
                    "message_id": "message-alias",
                })
            },
            "demo",
        )


def test_invalid_canonical_row_is_dlqd_before_source_ack(monkeypatch):
    async def scenario():
        writer, redis_client, ch_client = make_writer(monkeypatch)
        results = [
            (
                "events:raw:demo",
                [("1-0", {"event_json": json.dumps(canonical_event(["bad"]))})],
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
            "xdel",
        ]

    asyncio.run(scenario())


def test_duplicate_json_keys_are_rejected(monkeypatch):
    writer, _, _ = make_writer(monkeypatch)
    raw = (
        '{"event":"first","event":"second","type":"track",'
        '"anonymous_id":"anon","timestamp":"2026-07-13T12:00:00.000Z",'
        '"context":{},"message_id":"duplicate"}'
    )
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        writer._parse_event({"event_json": raw}, "demo")


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-07-13T12:00Z",
        "2026-07-13 12:00:00Z",
        "2026-07-13T12:00:00.1234567Z",
        "0999-01-01T00:00:00Z",
    ],
)
def test_noncanonical_timestamps_are_rejected(monkeypatch, timestamp):
    writer, _, _ = make_writer(monkeypatch)
    payload = canonical_event(timestamp=timestamp)

    with pytest.raises(ValueError, match="canonical RFC3339|canonical digits"):
        writer._parse_event({"event_json": json.dumps(payload)}, "demo")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", None),
        ("properties", None),
        ("traits", None),
        ("session_id", None),
    ],
)
def test_explicit_null_optional_fields_are_rejected(monkeypatch, field, value):
    writer, _, _ = make_writer(monkeypatch)
    payload = canonical_event(user_id="user-1")
    payload[field] = value

    with pytest.raises(ValueError, match="omitted rather than null"):
        writer._parse_event({"event_json": json.dumps(payload)}, "demo")


@pytest.mark.parametrize(
    "context",
    [
        {"locale": None},
        {"browser": {"name": "Firefox", "version": None}},
        {"device": {"type": ""}},
        {"screen": {"width": True, "height": 100}},
        {"page": {"url": 42, "title": "", "path": "/", "search": ""}},
    ],
)
def test_malformed_context_values_are_rejected(monkeypatch, context):
    writer, _, _ = make_writer(monkeypatch)
    payload = canonical_event(context=context)

    with pytest.raises((TypeError, ValueError)):
        writer._parse_event({"event_json": json.dumps(payload)}, "demo")


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
                    ("1-0", {"event_json": json.dumps(canonical_event(["bad"]))}),
                    ("2-0", {"event_json": json.dumps(canonical_event())}),
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
                [("1-0", {"event_json": json.dumps(canonical_event())})],
            ),
            (
                "events:raw:other",
                [("2-0", {"event_json": json.dumps(canonical_event("poison"))})],
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
                [("2-0", {"event_json": json.dumps(canonical_event("purchase"))})],
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
