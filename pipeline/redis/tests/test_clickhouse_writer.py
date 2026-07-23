import asyncio
import hashlib
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import clickhouse_writer as writer_module
import pytest
from clickhouse_driver.errors import ServerException, TypeMismatchError
from clickhouse_writer import BufferedEvent, ClickHouseWriter

CONTRACT_FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "events" / "canonical.json"
)


def test_each_maintenance_session_acquires_and_verifies_both_lock_ids() -> None:
    class GuardConnection:
        def __init__(self) -> None:
            self.acquired: list[int] = []
            self.heartbeat_query = ""

        async def execute(self, query: str, lock_id: int) -> None:
            assert query == "SELECT pg_advisory_lock_shared($1)"
            self.acquired.append(lock_id)

        async def fetchval(self, query: str, lock_ids: list[int]) -> int:
            self.heartbeat_query = query
            assert lock_ids == list(writer_module.MAINTENANCE_INHIBITOR_LOCK_IDS)
            return 2

    async def scenario() -> None:
        connection = GuardConnection()
        await writer_module._acquire_maintenance_inhibitor(connection)
        await writer_module._heartbeat_maintenance_inhibitor(connection)

        assert connection.acquired == [4_158_044_083, 4_158_044_084]
        assert "pid = pg_backend_pid()" in connection.heartbeat_query
        assert "objsubid = 1" in connection.heartbeat_query

    asyncio.run(scenario())


def test_boundary_marker_schema_gate_requires_exact_ledger_columns_and_state(
    monkeypatch,
):
    constraint_definitions = {
        name: f"canonical definition for {name}"
        for name in writer_module.BOUNDARY_MARKER_POSTGRES_CONSTRAINT_SHA256
    }
    function_definition = "canonical monotone terminal function"
    monkeypatch.setattr(
        writer_module,
        "BOUNDARY_MARKER_POSTGRES_CONSTRAINT_SHA256",
        {
            name: hashlib.sha256(definition.encode()).hexdigest()
            for name, definition in constraint_definitions.items()
        },
    )
    monkeypatch.setattr(
        writer_module,
        "BOUNDARY_MARKER_POSTGRES_FUNCTION_SHA256",
        hashlib.sha256(function_definition.encode()).hexdigest(),
    )

    class Connection:
        async def fetchval(self, query, *_args):
            assert "to_regclass" in query
            return True

        async def fetchrow(self, query, *args):
            if "apdl_schema_migrations" in query:
                assert args == (41,)
                return {
                    "name": writer_module.BOUNDARY_MARKER_POSTGRES_MIGRATION_NAME,
                    "checksum": (
                        writer_module.BOUNDARY_MARKER_POSTGRES_MIGRATION_SHA256
                    ),
                }
            assert "pg_catalog.pg_trigger" in query
            assert args == (
                writer_module.BOUNDARY_MARKER_POSTGRES_TRIGGER_NAME,
            )
            return {
                "tgname": writer_module.BOUNDARY_MARKER_POSTGRES_TRIGGER_NAME,
                "tgenabled": "O",
                "tgtype": 27,
                "tgisinternal": False,
                "trigger_definition": (
                    writer_module.BOUNDARY_MARKER_POSTGRES_TRIGGER_DEFINITION
                ),
                "function_schema": "public",
                "function_name": (
                    writer_module.BOUNDARY_MARKER_POSTGRES_FUNCTION_NAME
                ),
                "prokind": "f",
                "prosecdef": False,
                "proleakproof": False,
                "provolatile": "v",
                "proparallel": "u",
                "proconfig": ["search_path=pg_catalog, public"],
                "pronargs": 0,
                "return_type": "trigger",
                "function_definition": function_definition,
            }

        async def fetch(self, query, names):
            if "information_schema.columns" in query:
                expected = {
                    "marker_publish_state": (
                        "text",
                        "NO",
                        "'pending'::text",
                    ),
                    "marker_publish_attempts": ("int2", "NO", "0"),
                    "marker_publish_next_attempt_at": (
                        "timestamptz",
                        "YES",
                        "now()",
                    ),
                    "marker_publish_failure_code": ("text", "YES", None),
                    "marker_publish_last_error_at": (
                        "timestamptz",
                        "YES",
                        None,
                    ),
                    "marker_publish_quarantined_at": (
                        "timestamptz",
                        "YES",
                        None,
                    ),
                    "marker_publish_observed_stream_id": (
                        "text",
                        "YES",
                        None,
                    ),
                }
                assert names == sorted(expected)
                return [
                    {
                        "column_name": name,
                        "udt_name": values[0],
                        "is_nullable": values[1],
                        "column_default": values[2],
                    }
                    for name, values in expected.items()
                ]
            assert "pg_catalog.pg_constraint" in query
            assert names == sorted(constraint_definitions)
            return [
                {
                    "conname": name,
                    "contype": (
                        "u"
                        if name
                        == writer_module.BOUNDARY_MARKER_OBSERVED_IDENTITY_CONSTRAINT
                        else "c"
                    ),
                    "condeferrable": False,
                    "condeferred": False,
                    "convalidated": True,
                    "definition": definition,
                }
                for name, definition in constraint_definitions.items()
            ]

    asyncio.run(writer_module._assert_boundary_marker_schema(Connection()))


@pytest.mark.parametrize(
    ("drift", "error"),
    [
        ("constraint", "state constraint is not exact"),
        ("trigger", "state trigger is not exact"),
        ("function", "state trigger is not exact"),
    ],
)
def test_boundary_marker_schema_gate_rejects_state_machine_weakening(
    monkeypatch,
    drift,
    error,
):
    definitions = {
        name: f"exact-{name}"
        for name in writer_module.BOUNDARY_MARKER_POSTGRES_CONSTRAINT_SHA256
    }
    function_definition = "exact-function"
    monkeypatch.setattr(
        writer_module,
        "BOUNDARY_MARKER_POSTGRES_CONSTRAINT_SHA256",
        {
            name: hashlib.sha256(definition.encode()).hexdigest()
            for name, definition in definitions.items()
        },
    )
    monkeypatch.setattr(
        writer_module,
        "BOUNDARY_MARKER_POSTGRES_FUNCTION_SHA256",
        hashlib.sha256(function_definition.encode()).hexdigest(),
    )

    class Connection:
        async def fetchval(self, *_args):
            return True

        async def fetchrow(self, query, *_args):
            if "apdl_schema_migrations" in query:
                return {
                    "name": writer_module.BOUNDARY_MARKER_POSTGRES_MIGRATION_NAME,
                    "checksum": (
                        writer_module.BOUNDARY_MARKER_POSTGRES_MIGRATION_SHA256
                    ),
                }
            return {
                "tgname": writer_module.BOUNDARY_MARKER_POSTGRES_TRIGGER_NAME,
                "tgenabled": "O",
                "tgtype": 19 if drift == "trigger" else 27,
                "tgisinternal": False,
                "trigger_definition": (
                    writer_module.BOUNDARY_MARKER_POSTGRES_TRIGGER_DEFINITION
                ),
                "function_schema": "public",
                "function_name": (
                    writer_module.BOUNDARY_MARKER_POSTGRES_FUNCTION_NAME
                ),
                "prokind": "f",
                "prosecdef": False,
                "proleakproof": False,
                "provolatile": "v",
                "proparallel": "u",
                "proconfig": ["search_path=pg_catalog, public"],
                "pronargs": 0,
                "return_type": "trigger",
                "function_definition": (
                    function_definition + "-weakened"
                    if drift == "function"
                    else function_definition
                ),
            }

        async def fetch(self, query, names):
            if "information_schema.columns" in query:
                columns = {
                    "marker_publish_state": (
                        "text",
                        "NO",
                        "'pending'::text",
                    ),
                    "marker_publish_attempts": ("int2", "NO", "0"),
                    "marker_publish_next_attempt_at": (
                        "timestamptz",
                        "YES",
                        "now()",
                    ),
                    "marker_publish_failure_code": ("text", "YES", None),
                    "marker_publish_last_error_at": (
                        "timestamptz",
                        "YES",
                        None,
                    ),
                    "marker_publish_quarantined_at": (
                        "timestamptz",
                        "YES",
                        None,
                    ),
                    "marker_publish_observed_stream_id": (
                        "text",
                        "YES",
                        None,
                    ),
                }
                assert names == sorted(columns)
                return [
                    {
                        "column_name": name,
                        "udt_name": values[0],
                        "is_nullable": values[1],
                        "column_default": values[2],
                    }
                    for name, values in columns.items()
                ]
            weakened_name = next(iter(definitions))
            return [
                {
                    "conname": name,
                    "contype": (
                        "u"
                        if name
                        == writer_module.BOUNDARY_MARKER_OBSERVED_IDENTITY_CONSTRAINT
                        else "c"
                    ),
                    "condeferrable": False,
                    "condeferred": False,
                    "convalidated": True,
                    "definition": (
                        definition + "-weakened"
                        if drift == "constraint" and name == weakened_name
                        else definition
                    ),
                }
                for name, definition in definitions.items()
            ]

    with pytest.raises(RuntimeError, match=error):
        asyncio.run(writer_module._assert_boundary_marker_schema(Connection()))


def test_boundary_marker_schema_gate_checksum_matches_migration():
    migration = (
        Path(__file__).resolve().parents[2]
        / "postgres"
        / "migrations"
        / writer_module.BOUNDARY_MARKER_POSTGRES_MIGRATION_NAME
    )
    assert hashlib.sha256(migration.read_bytes()).hexdigest() == (
        writer_module.BOUNDARY_MARKER_POSTGRES_MIGRATION_SHA256
    )


def test_boundary_marker_schema_gate_rejects_wrong_ledger_checksum():
    class Connection:
        async def fetchval(self, query, *_args):
            assert "to_regclass" in query
            return True

        async def fetchrow(self, query, version):
            assert "apdl_schema_migrations" in query
            assert version == 41
            return {
                "name": writer_module.BOUNDARY_MARKER_POSTGRES_MIGRATION_NAME,
                "checksum": "0" * 64,
            }

        async def fetch(self, *_args):
            raise AssertionError("column checks must follow exact ledger proof")

    with pytest.raises(RuntimeError, match="migration is not exact"):
        asyncio.run(
            writer_module._assert_boundary_marker_schema(Connection())
        )


def test_writer_schema_gate_failure_precedes_and_unwinds_runtime_locks(
    monkeypatch,
):
    class Pool:
        def __init__(self):
            self.connection = object()
            self.released = []
            self.closed = False

        async def acquire(self):
            return self.connection

        async def release(self, connection):
            self.released.append(connection)

        async def close(self):
            self.closed = True

    async def scenario():
        pool = Pool()
        singleton_calls = []
        inhibitor_calls = []

        async def create_pool(*_args, **_kwargs):
            return pool

        async def fail_schema(_connection):
            raise RuntimeError("migration 041 is absent")

        async def record_singleton(*_args, **_kwargs):
            singleton_calls.append(True)

        async def record_inhibitor(*_args, **_kwargs):
            inhibitor_calls.append(True)

        monkeypatch.setattr(writer_module.asyncpg, "create_pool", create_pool)
        monkeypatch.setattr(
            writer_module,
            "_assert_boundary_marker_schema",
            fail_schema,
        )
        monkeypatch.setattr(
            writer_module,
            "_acquire_writer_singleton",
            record_singleton,
        )
        monkeypatch.setattr(
            writer_module,
            "_acquire_maintenance_inhibitor",
            record_inhibitor,
        )

        with pytest.raises(RuntimeError, match="migration 041 is absent"):
            await writer_module.main()

        assert inhibitor_calls == [True]
        assert singleton_calls == []
        assert pool.released == [pool.connection]
        assert pool.closed is True

    asyncio.run(scenario())


def test_maintenance_inhibitor_loss_stops_writer() -> None:
    class LostConnection:
        async def fetchval(self, _query: str, _lock_ids: list[int]) -> None:
            raise ConnectionError("postgres connection lost")

    class StoppableWriter:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self, *, flush_buffer: bool = True) -> None:
            assert flush_buffer is False
            self.stopped = True

    async def scenario() -> None:
        writer = StoppableWriter()
        await asyncio.wait_for(
            writer_module._monitor_maintenance_inhibitor(
                LostConnection(),
                writer,
                asyncio.Event(),
                heartbeat_seconds=0.001,
            ),
            timeout=0.1,
        )
        assert writer.stopped is True

    asyncio.run(scenario())


def test_loss_of_one_inhibitor_keeps_the_redundant_guard_alive_during_drain() -> None:
    class GuardConnection:
        def __init__(self) -> None:
            self.heartbeats = 0

        async def fetchval(self, _query: str, lock_ids: list[int]) -> int:
            assert lock_ids == list(writer_module.MAINTENANCE_INHIBITOR_LOCK_IDS)
            self.heartbeats += 1
            return 2

    class DrainingWriter:
        def __init__(self) -> None:
            self.drain_started = asyncio.Event()
            self.allow_drain = asyncio.Event()

        async def stop(self, *, flush_buffer: bool = True) -> None:
            assert flush_buffer is False
            self.drain_started.set()
            await self.allow_drain.wait()

    async def scenario() -> None:
        writer = DrainingWriter()
        lost_event = asyncio.Event()
        lost_event.set()
        surviving_connection = GuardConnection()
        lost_monitor = asyncio.create_task(
            writer_module._monitor_maintenance_inhibitor(
                GuardConnection(),
                writer,
                lost_event,
                heartbeat_seconds=0.001,
            )
        )
        surviving_monitor = asyncio.create_task(
            writer_module._monitor_maintenance_inhibitor(
                surviving_connection,
                writer,
                asyncio.Event(),
                heartbeat_seconds=0.001,
            )
        )

        await asyncio.wait_for(writer.drain_started.wait(), timeout=0.1)
        while surviving_connection.heartbeats == 0:
            await asyncio.sleep(0)
        assert lost_monitor.done() is False
        assert surviving_monitor.done() is False

        writer.allow_drain.set()
        await asyncio.wait_for(lost_monitor, timeout=0.1)
        surviving_monitor.cancel()
        await asyncio.gather(surviving_monitor, return_exceptions=True)

    asyncio.run(scenario())


def test_hung_inhibitor_heartbeat_is_bounded_and_fences_writes() -> None:
    class HungConnection:
        async def fetchval(self, _query: str, _lock_ids: list[int]) -> None:
            await asyncio.Event().wait()

    class StoppableWriter:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self, *, flush_buffer: bool = True) -> None:
            assert flush_buffer is False
            self.stopped = True

    async def scenario() -> None:
        writer = StoppableWriter()
        await asyncio.wait_for(
            writer_module._monitor_maintenance_inhibitor(
                HungConnection(),
                writer,
                asyncio.Event(),
                heartbeat_seconds=0.001,
            ),
            timeout=0.1,
        )
        assert writer.stopped is True

    asyncio.run(scenario())


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
        self.stream_info: dict[str, dict] = {}
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
        self.scan_calls: list[dict] = []
        self.scan_responses: list[tuple[int, list[str]]] = []
        self.closed = False

    def pipeline(self, *, transaction):
        self.pipeline_transactions.append(transaction)
        return FakePipeline(self, transaction=transaction)

    async def xreadgroup(self, **kwargs):
        self.read_calls += 1
        self.read_args.append(kwargs)
        return []

    async def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        if self.scan_responses:
            return self.scan_responses.pop(0)
        return 0, []

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
                    "entries-read": 0,
                }
            ],
        )

    async def xinfo_stream(self, stream_key):
        return self.stream_info.get(
            stream_key,
            {"max-deleted-entry-id": "0-0"},
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

    async def aclose(self):
        self.closed = True


def external_event_rows(external_tables):
    assert len(external_tables) == 1
    table = external_tables[0]
    assert table["name"] == "apdl_runtime_input"
    assert table["structure"] == list(writer_module.EVENT_INPUT_STRUCTURE)
    return table["data"]


class FakeClickHouse:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.inserts: list[list[dict]] = []
        self.query_ids: list[str] = []
        self.disconnected = False

    def execute(
        self,
        _query,
        *,
        external_tables=None,
        query_id=None,
        **_kwargs,
    ):
        assert _query == writer_module.EVENT_INSERT_QUERY
        assert query_id is not None
        rows = external_event_rows(external_tables)
        self.query_ids.append(query_id)
        self.inserts.append(rows)
        if self.fail:
            raise RuntimeError("clickhouse unavailable")

    def disconnect(self):
        self.disconnected = True


class FakeClickHouseControl:
    def __init__(self):
        self.killed_query_ids: list[str] = []
        self.inspected_query_ids: list[str] = []
        self.disconnected = False

    def execute(self, query, params=None, **_kwargs):
        query_id = params["query_id"]
        if query.startswith("KILL QUERY"):
            self.killed_query_ids.append(query_id)
            return []
        if "system.processes" in query:
            self.inspected_query_ids.append(query_id)
            return [(0,)]
        raise AssertionError(f"Unexpected ClickHouse control query: {query}")

    def disconnect(self):
        self.disconnected = True


def make_writer(
    monkeypatch,
    *,
    redis_client=None,
    ch_client=None,
    ch_control_client=None,
    buffer_size=10,
    **writer_kwargs,
):
    redis_client = redis_client or FakeRedis()
    ch_client = ch_client or FakeClickHouse()
    ch_control_client = ch_control_client or FakeClickHouseControl()
    clickhouse_clients = iter((ch_client, ch_control_client))
    monkeypatch.setattr(
        writer_module.redis, "from_url", lambda *_args, **_kwargs: redis_client
    )
    monkeypatch.setattr(
        writer_module.ClickHouseClient,
        "from_url",
        lambda *_args, **_kwargs: next(clickhouse_clients),
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
        "server_timestamp": "2026-07-13T12:00:00.000Z",
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


async def wait_for_thread_event(event: threading.Event) -> None:
    deadline = asyncio.get_running_loop().time() + 1.0
    while not event.is_set():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("worker thread did not start")
        await asyncio.sleep(0)


def test_clickhouse_driver_receives_explicit_process_timeouts(monkeypatch):
    redis_client = FakeRedis()
    ch_client = FakeClickHouse()
    ch_control_client = FakeClickHouseControl()
    captured_urls = []
    clickhouse_clients = iter((ch_client, ch_control_client))
    monkeypatch.setattr(
        writer_module.redis, "from_url", lambda *_args, **_kwargs: redis_client
    )
    monkeypatch.setattr(
        writer_module.ClickHouseClient,
        "from_url",
        lambda url: captured_urls.append(url) or next(clickhouse_clients),
    )

    writer = ClickHouseWriter(
        "redis://test",
        "clickhouse://test/apdl?connect_timeout=999&compression=true",
        clickhouse_connect_timeout=1.5,
        clickhouse_send_receive_timeout=12.0,
        clickhouse_sync_request_timeout=2.5,
    )
    assert len(captured_urls) == 2
    assert captured_urls[0] == captured_urls[1]
    query = parse_qs(urlsplit(captured_urls[0]).query)

    assert query == {
        "compression": ["true"],
        "connect_timeout": ["1.5"],
        "send_receive_timeout": ["12.0"],
        "sync_request_timeout": ["2.5"],
    }
    asyncio.run(writer.stop())


def test_slow_clickhouse_insert_does_not_block_event_loop(monkeypatch):
    class SlowClickHouse(FakeClickHouse):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def execute(self, query, **kwargs):
            self.started.set()
            assert self.release.wait(timeout=1.0)
            super().execute(query, **kwargs)

        def disconnect(self):
            self.release.set()
            super().disconnect()

    async def scenario():
        ch_client = SlowClickHouse()
        writer, _, _ = make_writer(monkeypatch, ch_client=ch_client)
        writer.buffer.append(buffered_event())

        flush_task = asyncio.create_task(writer._flush())
        await wait_for_thread_event(ch_client.started)

        loop_progressed = False

        async def mark_progress():
            nonlocal loop_progressed
            await asyncio.sleep(0)
            loop_progressed = True

        await asyncio.wait_for(mark_progress(), timeout=0.1)
        assert loop_progressed is True

        ch_client.release.set()
        assert await flush_task is True
        await writer.stop()

    asyncio.run(scenario())


def test_cancelled_flush_observes_original_insert_before_retrying(monkeypatch):
    class SlowClickHouse(FakeClickHouse):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def execute(self, query, **kwargs):
            self.started.set()
            assert self.release.wait(timeout=1.0)
            super().execute(query, **kwargs)

        def disconnect(self):
            self.release.set()
            super().disconnect()

    async def scenario():
        ch_client = SlowClickHouse()
        writer, redis_client, _ = make_writer(monkeypatch, ch_client=ch_client)
        writer.buffer.append(buffered_event())

        flush_task = asyncio.create_task(writer._flush())
        await wait_for_thread_event(ch_client.started)
        flush_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await flush_task

        assert writer.buffer == [buffered_event()]
        assert redis_client.acks == []

        ch_client.release.set()
        assert await writer._flush() is True
        assert len(ch_client.inserts) == 1
        assert len(ch_client.query_ids) == 1
        assert redis_client.acks == [("events:raw:demo", "clickhouse-writer", ("1-0",))]
        await writer.stop()

    asyncio.run(scenario())


def test_every_native_insert_gets_a_unique_stable_query_id(monkeypatch):
    async def scenario():
        writer, _, ch_client = make_writer(monkeypatch)
        writer.buffer.append(buffered_event("1-0"))
        assert await writer._flush() is True
        writer.buffer.append(buffered_event("2-0"))
        assert await writer._flush() is True

        assert len(ch_client.query_ids) == 2
        assert len(set(ch_client.query_ids)) == 2
        assert all(
            query_id.startswith("apdl-runtime-writer-")
            for query_id in ch_client.query_ids
        )
        await writer.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("gate_state", "expected_success"),
    (("open", True), ("blocked", False), ("missing", False)),
)
def test_insert_requires_the_exact_durable_maintenance_gate(
    monkeypatch,
    gate_state,
    expected_success,
):
    class GateAwareClickHouse(FakeClickHouse):
        def __init__(self):
            super().__init__()
            self.queries: list[str] = []
            self.external_table_payloads: list[list[dict]] = []

        def execute(
            self,
            query,
            *,
            external_tables=None,
            query_id=None,
            **kwargs,
        ):
            self.queries.append(query)
            self.external_table_payloads.append(external_tables)
            external_event_rows(external_tables)
            if gate_state == "blocked":
                raise ServerException("maintenance", code=395)
            if gate_state == "missing":
                raise ServerException("unknown table", code=60)
            return super().execute(
                query,
                external_tables=external_tables,
                query_id=query_id,
                **kwargs,
            )

    async def scenario():
        ch_client = GateAwareClickHouse()
        writer, redis_client, _ = make_writer(monkeypatch, ch_client=ch_client)
        delivery = buffered_event()
        writer.buffer.append(delivery)

        assert await writer._flush() is expected_success
        assert ch_client.queries == [writer_module.EVENT_INSERT_QUERY]
        assert "FROM apdl_runtime_input" in ch_client.queries[0]
        assert len(ch_client.external_table_payloads) == 1
        external_table = ch_client.external_table_payloads[0][0]
        assert external_table["name"] == "apdl_runtime_input"
        assert external_table["structure"] == list(writer_module.EVENT_INPUT_STRUCTURE)
        assert external_table["data"] == [delivery.row]
        assert "SELECT (count() = 0) OR" in ch_client.queries[0]
        assert "argMax(writes_blocked, generation) != 0" in ch_client.queries[0]
        assert "authority = 'runtime-writes'" in ch_client.queries[0]

        if expected_success:
            assert writer.buffer == []
            assert redis_client.acks == [
                ("events:raw:demo", "clickhouse-writer", ("1-0",))
            ]
        else:
            assert writer.buffer == [delivery]
            assert redis_client.acks == []
            assert writer.stats["errors"] == 1

        await writer.stop(flush_buffer=False)

    asyncio.run(scenario())


def test_executor_rechecks_insert_gate_after_work_was_queued(monkeypatch):
    writer, _, ch_client = make_writer(monkeypatch)
    writer._accepting_inserts = False

    with pytest.raises(RuntimeError, match="INSERTs are fenced"):
        writer._execute_insert([buffered_event()], "apdl-runtime-writer-queued")

    assert ch_client.inserts == []
    asyncio.run(writer.stop(flush_buffer=False))


def test_shutdown_kills_registered_insert_and_proves_it_absent(monkeypatch):
    class RegisteredInsertClickHouse(FakeClickHouse):
        def __init__(self):
            super().__init__()
            self.active_query_ids: set[str] = set()
            self.started = threading.Event()
            self.killed = threading.Event()

        def execute(
            self,
            _query,
            *,
            external_tables=None,
            query_id=None,
            **_kwargs,
        ):
            assert query_id is not None
            rows = external_event_rows(external_tables)
            self.query_ids.append(query_id)
            self.inserts.append(rows)
            self.active_query_ids.add(query_id)
            self.started.set()
            assert self.killed.wait(timeout=1.0)
            raise RuntimeError("query killed")

        def disconnect(self):
            self.disconnected = True

    class RegisteredQueryControl(FakeClickHouseControl):
        def __init__(self, data_client):
            super().__init__()
            self.data_client = data_client

        def execute(self, query, params=None, **_kwargs):
            query_id = params["query_id"]
            if query.startswith("KILL QUERY"):
                assert query_id in self.data_client.active_query_ids
                self.killed_query_ids.append(query_id)
                self.data_client.active_query_ids.remove(query_id)
                self.data_client.killed.set()
                return []
            if "system.processes" in query:
                self.inspected_query_ids.append(query_id)
                return [(int(query_id in self.data_client.active_query_ids),)]
            raise AssertionError(f"Unexpected ClickHouse control query: {query}")

    async def scenario():
        ch_client = RegisteredInsertClickHouse()
        control_client = RegisteredQueryControl(ch_client)
        writer, redis_client, _ = make_writer(
            monkeypatch,
            ch_client=ch_client,
            ch_control_client=control_client,
            shutdown_timeout=0.01,
        )
        writer.buffer.append(buffered_event())

        flush_task = asyncio.create_task(writer._flush())
        await wait_for_thread_event(ch_client.started)
        await writer.stop()

        assert await asyncio.wait_for(flush_task, timeout=0.2) is False
        assert control_client.killed_query_ids == ch_client.query_ids
        assert control_client.inspected_query_ids == ch_client.query_ids
        assert ch_client.active_query_ids == set()
        assert redis_client.acks == []
        assert writer.buffer == [buffered_event()]

    asyncio.run(scenario())


def test_shutdown_waits_out_query_registration_race_before_releasing(monkeypatch):
    class LateRegisteringClickHouse(FakeClickHouse):
        def __init__(self):
            super().__init__()
            self.thread_started = threading.Event()
            self.allow_registration = threading.Event()
            self.registered = threading.Event()
            self.killed = threading.Event()
            self.active_query_ids: set[str] = set()

        def execute(
            self,
            _query,
            *,
            external_tables=None,
            query_id=None,
            **_kwargs,
        ):
            assert query_id is not None
            rows = external_event_rows(external_tables)
            self.query_ids.append(query_id)
            self.inserts.append(rows)
            self.thread_started.set()
            assert self.allow_registration.wait(timeout=1.0)
            self.active_query_ids.add(query_id)
            self.registered.set()
            assert self.killed.wait(timeout=1.0)
            raise RuntimeError("query killed")

        def disconnect(self):
            self.disconnected = True

    class RegistrationRaceControl(FakeClickHouseControl):
        def __init__(self, data_client):
            super().__init__()
            self.data_client = data_client
            self.first_kill = threading.Event()

        def execute(self, query, params=None, **_kwargs):
            query_id = params["query_id"]
            if query.startswith("KILL QUERY"):
                self.killed_query_ids.append(query_id)
                self.first_kill.set()
                if query_id in self.data_client.active_query_ids:
                    self.data_client.active_query_ids.remove(query_id)
                    self.data_client.killed.set()
                return []
            if "system.processes" in query:
                self.inspected_query_ids.append(query_id)
                return [(int(query_id in self.data_client.active_query_ids),)]
            raise AssertionError(f"Unexpected ClickHouse control query: {query}")

    async def scenario():
        ch_client = LateRegisteringClickHouse()
        control_client = RegistrationRaceControl(ch_client)
        writer, redis_client, _ = make_writer(
            monkeypatch,
            ch_client=ch_client,
            ch_control_client=control_client,
            shutdown_timeout=0.01,
        )
        writer.buffer.append(buffered_event())
        flush_task = asyncio.create_task(writer._flush())
        await wait_for_thread_event(ch_client.thread_started)

        stop_task = asyncio.create_task(writer.stop())
        await wait_for_thread_event(control_client.first_kill)
        assert writer._closed is False
        assert writer._inflight_insert is not None
        assert control_client.inspected_query_ids == []

        ch_client.allow_registration.set()
        await wait_for_thread_event(ch_client.registered)
        await asyncio.wait_for(stop_task, timeout=0.5)

        assert writer._closed is True
        assert ch_client.active_query_ids == set()
        assert control_client.inspected_query_ids == ch_client.query_ids
        assert redis_client.acks == []
        assert await asyncio.wait_for(flush_task, timeout=0.2) is False

    asyncio.run(scenario())


def test_cancellation_during_unproven_shutdown_keeps_retrying(monkeypatch):
    class AbortableInsertClickHouse(FakeClickHouse):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def execute(
            self,
            _query,
            *,
            external_tables=None,
            query_id=None,
            **_kwargs,
        ):
            assert query_id is not None
            rows = external_event_rows(external_tables)
            self.query_ids.append(query_id)
            self.inserts.append(rows)
            self.started.set()
            assert self.release.wait(timeout=1.0)
            raise RuntimeError("data connection closed")

        def disconnect(self):
            self.disconnected = True
            self.release.set()

    class RetryingControl(FakeClickHouseControl):
        def __init__(self):
            super().__init__()
            self.first_inspection = threading.Event()
            self.allow_absence = threading.Event()

        def execute(self, query, params=None, **_kwargs):
            query_id = params["query_id"]
            if query.startswith("KILL QUERY"):
                self.killed_query_ids.append(query_id)
                return []
            if "system.processes" in query:
                self.inspected_query_ids.append(query_id)
                self.first_inspection.set()
                return [(0 if self.allow_absence.is_set() else 1,)]
            raise AssertionError(f"Unexpected ClickHouse control query: {query}")

    async def scenario():
        ch_client = AbortableInsertClickHouse()
        control_client = RetryingControl()
        writer, redis_client, _ = make_writer(
            monkeypatch,
            ch_client=ch_client,
            ch_control_client=control_client,
            shutdown_timeout=0.01,
        )
        writer.buffer.append(buffered_event())
        flush_task = asyncio.create_task(writer._flush())
        await wait_for_thread_event(ch_client.started)

        stop_task = asyncio.create_task(writer.stop())
        await wait_for_thread_event(control_client.first_inspection)
        stop_task.cancel()
        await asyncio.sleep(0)

        assert writer._closed is False
        assert writer._inflight_insert is not None
        assert redis_client.closed is False

        control_client.allow_absence.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop_task, timeout=0.5)

        assert writer._closed is True
        assert writer._inflight_insert is None
        assert redis_client.acks == []
        assert writer.buffer == [buffered_event()]
        assert len(control_client.killed_query_ids) >= 2
        assert await asyncio.wait_for(flush_task, timeout=0.2) is False

    asyncio.run(scenario())


def test_shutdown_aborts_slow_insert_and_leaves_delivery_pending(monkeypatch):
    class AbortableSlowClickHouse(FakeClickHouse):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def execute(self, _query, *, external_tables=None, **_kwargs):
            rows = external_event_rows(external_tables)
            self.inserts.append(rows)
            self.started.set()
            assert self.release.wait(timeout=1.0)
            raise RuntimeError("insert aborted")

        def disconnect(self):
            self.disconnected = True
            self.release.set()

    async def scenario():
        ch_client = AbortableSlowClickHouse()
        writer, redis_client, _ = make_writer(
            monkeypatch,
            ch_client=ch_client,
            shutdown_timeout=0.01,
        )
        writer.buffer.append(buffered_event())

        flush_task = asyncio.create_task(writer._flush())
        await wait_for_thread_event(ch_client.started)
        started_at = asyncio.get_running_loop().time()
        await writer.stop()
        elapsed = asyncio.get_running_loop().time() - started_at

        assert elapsed < 0.2
        assert ch_client.disconnected is True
        assert redis_client.closed is True
        assert redis_client.acks == []
        assert writer.buffer == [buffered_event()]
        assert await asyncio.wait_for(flush_task, timeout=0.2) is False

    asyncio.run(scenario())


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
    """A stable Redis delivery ID collapses an insert-before-ACK replay."""

    class IdempotentClickHouse(FakeClickHouse):
        def __init__(self):
            super().__init__()
            self.rows: dict[tuple[str, str, str], dict] = {}

        def execute(self, _query, *, external_tables=None, **_kwargs):
            rows = external_event_rows(external_tables)
            self.inserts.append(rows)
            for row in rows:
                self.rows[
                    (
                        row["project_id"],
                        row["source_stream"],
                        row["source_stream_id"],
                    )
                ] = row

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
        assert list(clickhouse.rows) == [("demo", "events:raw:demo", "1-0")]
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


def test_stream_pressure_logs_exact_values_at_a_bounded_interval(monkeypatch, caplog):
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
        assert redis_client.group_creates == []

    asyncio.run(scenario())


def test_existing_consumer_group_avoids_group_creation_write(monkeypatch):
    async def scenario():
        writer, redis_client, _ = make_writer(monkeypatch)

        await writer._ensure_consumer_groups(["demo"])

        assert redis_client.group_creates == []
        assert redis_client.xinfo_group_calls == ["events:raw:demo"]

    asyncio.run(scenario())


def test_dynamic_start_forces_discovery_before_group_creation(monkeypatch):
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
        monkeypatch.setattr(writer, "_stream_discovery_loop", no_op)

        await writer.start()

        assert calls[:5] == [
            ("discover",),
            ("reconcile", ("events:raw:demo",), False),
            ("ensure", ()),
            ("get", ()),
            ("reconcile", ("events:raw:demo",), True),
        ]

    asyncio.run(scenario())


def test_fixed_project_start_never_scans_global_keyspace(monkeypatch):
    async def scenario():
        writer, redis_client, _ = make_writer(monkeypatch)

        async def no_op(*_args, **_kwargs):
            return None

        monkeypatch.setattr(writer, "_consume_loop", no_op)
        monkeypatch.setattr(writer, "_flush_loop", no_op)
        monkeypatch.setattr(writer, "_monitor_loop", no_op)

        await writer.start(["demo"])

        assert redis_client.scan_calls == []
        assert writer._known_stream_keys == {"events:raw:demo"}

    asyncio.run(scenario())


def test_discovery_loop_refreshes_registry_on_the_configured_interval(monkeypatch):
    async def scenario():
        writer, _, _ = make_writer(
            monkeypatch,
            stream_discovery_interval=0.001,
        )
        refresh_calls = 0

        async def refresh():
            nonlocal refresh_calls
            refresh_calls += 1
            writer.running = False
            return ["events:raw:new"]

        monkeypatch.setattr(writer, "_refresh_discovered_stream_registry", refresh)
        writer.running = True

        await asyncio.wait_for(writer._stream_discovery_loop(), timeout=0.1)

        assert refresh_calls == 1

    asyncio.run(scenario())


def test_successful_discovery_refresh_atomically_publishes_new_snapshot(
    monkeypatch,
):
    async def scenario():
        writer, _, _ = make_writer(monkeypatch)
        writer._replace_stream_registry(["events:raw:old"])
        calls = []

        async def discover():
            calls.append(("discover",))
            return ["events:raw:new", "events:raw:old"]

        async def reconcile(stream_keys, *, require_group=True):
            calls.append(("reconcile", tuple(stream_keys), require_group))

        async def ensure(stream_keys):
            calls.append(("ensure", tuple(stream_keys)))

        monkeypatch.setattr(writer, "_discover_streams", discover)
        monkeypatch.setattr(writer, "_reconcile_acknowledged_history", reconcile)
        monkeypatch.setattr(writer, "_ensure_consumer_groups_for_streams", ensure)

        snapshot = await writer._refresh_discovered_stream_registry()

        assert snapshot == ["events:raw:new", "events:raw:old"]
        assert await writer._get_stream_keys(None) == snapshot
        assert calls == [
            ("discover",),
            (
                "reconcile",
                ("events:raw:new", "events:raw:old"),
                False,
            ),
            ("ensure", ("events:raw:new", "events:raw:old")),
        ]

    asyncio.run(scenario())


def test_failed_discovery_retains_last_valid_registry_snapshot(monkeypatch):
    async def scenario():
        writer, _, _ = make_writer(monkeypatch)
        writer._replace_stream_registry(["events:raw:stable"])
        writer._last_stream_pressure_log["events:raw:stable"] = 123.0

        async def unavailable():
            raise ConnectionError("redis scan unavailable")

        monkeypatch.setattr(writer, "_discover_streams", unavailable)

        with pytest.raises(ConnectionError, match="scan unavailable"):
            await writer._refresh_discovered_stream_registry()

        assert await writer._get_stream_keys(None) == ["events:raw:stable"]
        assert writer._last_stream_pressure_log == {
            "events:raw:stable": 123.0,
        }

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
        payload = {**event, "server_timestamp": event["timestamp"]}
        row = writer._parse_event({"event_json": json.dumps(payload)}, "demo")
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


def test_writer_preserves_both_ids_on_identify_alias_assertion(monkeypatch):
    writer, _, _ = make_writer(monkeypatch)
    payload = canonical_event(
        event="identify",
        type="identify",
        user_id="user-1",
        anonymous_id="anon-1",
        message_id="message-identify-alias",
    )

    row = writer._parse_event({"event_json": json.dumps(payload)}, "demo")

    assert row["project_id"] == "demo"
    assert row["event_type"] == "identify"
    assert row["user_id"] == "user-1"
    assert row["anonymous_id"] == "anon-1"


def test_writer_keeps_user_only_identify_distinct_from_alias_assertion(monkeypatch):
    writer, _, _ = make_writer(monkeypatch)
    payload = canonical_event(
        event="identify",
        type="identify",
        user_id="user-1",
        message_id="message-identify-traits",
    )
    payload.pop("anonymous_id")

    row = writer._parse_event({"event_json": json.dumps(payload)}, "demo")

    assert row["event_type"] == "identify"
    assert row["user_id"] == "user-1"
    assert row["anonymous_id"] == ""


def test_legacy_alias_event_is_rejected(monkeypatch):
    writer, _, _ = make_writer(monkeypatch)

    with pytest.raises(ValueError, match="unknown fields"):
        writer._parse_event(
            {
                "event_json": json.dumps(
                    {
                        "event": "identify",
                        "type": "identify",
                        "userId": "user-1",
                        "anonymousId": "anon-1",
                        "timestamp": "2026-07-13T12:00:00.000Z",
                        "context": {},
                        "message_id": "message-alias",
                    }
                )
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


def test_out_of_window_event_time_is_quarantined_without_rewrite(monkeypatch):
    async def scenario():
        writer, redis_client, ch_client = make_writer(monkeypatch)
        event = canonical_event(timestamp="2026-07-06T11:59:59.999999Z")
        results = [
            (
                "events:raw:demo",
                [("1-0", {"event_json": json.dumps(event)})],
            )
        ]

        assert await writer._process_messages(results) == 0
        assert ch_client.inserts == []
        assert event["timestamp"] == "2026-07-06T11:59:59.999999Z"
        assert len(redis_client.adds) == 1
        dlq_stream, fields, _ = redis_client.adds[0]
        assert dlq_stream == "events:dlq:demo"
        assert fields["reason_code"] == "invalid_event_schema"
        assert fields["error_type"] == "ValueError"
        assert redis_client.acks == []

        assert await writer._flush() is True
        assert redis_client.acks == [
            ("events:raw:demo", "clickhouse-writer", ("1-0",))
        ]

    asyncio.run(scenario())


def test_missing_server_timestamp_is_quarantined_before_source_ack(monkeypatch):
    async def scenario():
        writer, redis_client, ch_client = make_writer(monkeypatch)
        event = canonical_event()
        event.pop("server_timestamp")

        assert await writer._process_messages([
            (
                "events:raw:demo",
                [("1-0", {"event_json": json.dumps(event)})],
            )
        ]) == 0
        assert ch_client.inserts == []
        assert len(redis_client.adds) == 1
        _, fields, _ = redis_client.adds[0]
        assert fields["reason_code"] == "invalid_event_schema"
        assert fields["error_type"] == "ValueError"
        assert redis_client.acks == []

        assert await writer._flush() is True
        assert redis_client.acks == [
            ("events:raw:demo", "clickhouse-writer", ("1-0",))
        ]

    asyncio.run(scenario())


def test_stream_delivery_provenance_is_written_to_clickhouse_row(monkeypatch):
    writer, _, _ = make_writer(monkeypatch)

    row = writer._parse_event(
        {"event_json": json.dumps(canonical_event())},
        "demo",
        source_stream="events:raw:demo",
        source_stream_id="1738281601000-7",
    )

    assert row["source_stream"] == "events:raw:demo"
    assert row["source_stream_id"] == "1738281601000-7"
    assert row["source_stream_id_ms"] == 1_738_281_601_000
    assert row["source_stream_id_seq"] == 7


def test_boundary_marker_contract_is_strict():
    token = "a" * 64
    assert (
        ClickHouseWriter._parse_boundary_marker(
            {
                "message_kind": "experiment_analysis_boundary",
                "boundary_token": token,
            }
        )
        == token
    )
    with pytest.raises(ValueError, match="fields are not canonical"):
        ClickHouseWriter._parse_boundary_marker(
            {
                "message_kind": "experiment_analysis_boundary",
                "boundary_token": token,
                "event_json": "{}",
            }
        )


def test_boundary_marker_publication_isolated_and_fair_per_project(monkeypatch):
    class Connection:
        def __init__(self):
            self.query = ""

        async def fetch(self, query, project_ids):
            self.query = query
            assert project_ids is None
            return [
                {
                    "project_id": "blocked",
                    "experiment_key": "old",
                    "config_version": 1,
                    "stream_key": "events:raw:blocked",
                    "marker_token": "a" * 64,
                    "marker_publish_attempts": 0,
                    "marker_publish_observed_stream_id": None,
                },
                {
                    "project_id": "healthy",
                    "experiment_key": "ready",
                    "config_version": 1,
                    "stream_key": "events:raw:healthy",
                    "marker_token": "b" * 64,
                    "marker_publish_attempts": 0,
                    "marker_publish_observed_stream_id": None,
                },
            ]

    class Acquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def __init__(self, connection):
            self.connection = connection

        def acquire(self):
            return Acquire(self.connection)

    async def scenario():
        connection = Connection()
        writer, _, _ = make_writer(
            monkeypatch,
            authority_pool=Pool(connection),
        )
        attempted = []

        async def publish(marker):
            attempted.append(marker.project_id)
            if marker.project_id == "blocked":
                raise writer_module.BoundaryMarkerPublishError(
                    "redis_publish_failed",
                    terminal=False,
                )

        async def persist_failure(marker, failure):
            assert marker.project_id == "blocked"
            assert failure.code == "redis_publish_failed"
            return "pending"

        monkeypatch.setattr(writer, "_publish_one_boundary_marker", publish)
        monkeypatch.setattr(
            writer,
            "_record_boundary_marker_failure",
            persist_failure,
        )

        await writer._publish_pending_boundary_markers(None)

        assert attempted == ["blocked", "healthy"]
        assert "PARTITION BY project_id" in connection.query
        assert "WHERE project_rank = 1" in connection.query
        assert "marker_publish_next_attempt_at <= clock_timestamp()" in (
            connection.query
        )
        assert writer.stats["boundary_markers_retried"] == 1
        assert writer.stats["boundary_markers_published"] == 1
        assert writer.stats["errors"] == 1

    asyncio.run(scenario())


def test_boundary_marker_retry_schedule_is_bounded():
    assert [
        ClickHouseWriter._boundary_marker_retry_delay(attempt)
        for attempt in range(1, 5)
    ] == [1, 2, 4, 8]
    with pytest.raises(ValueError, match="out of range"):
        ClickHouseWriter._boundary_marker_retry_delay(5)


@pytest.mark.parametrize(
    ("attempts", "failure_code", "terminal"),
    [
        (4, "event_stream_capacity", False),
        (0, "invalid_marker_token", True),
    ],
)
def test_boundary_marker_exhaustion_or_malformed_input_is_quarantined(
    monkeypatch,
    attempts,
    failure_code,
    terminal,
):
    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Connection:
        def __init__(self):
            self.call = None

        def transaction(self):
            return Transaction()

        async def fetchrow(self, query, *args):
            self.call = (query, args)
            return {
                "marker_publish_state": "quarantined",
                "marker_publish_attempts": attempts + 1,
                "marker_publish_observed_stream_id": None,
            }

    class Acquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def __init__(self, connection):
            self.connection = connection

        def acquire(self):
            return Acquire(self.connection)

    async def scenario():
        connection = Connection()
        writer, _, _ = make_writer(
            monkeypatch,
            authority_pool=Pool(connection),
        )
        marker = writer_module.PendingBoundaryMarker(
            project_id="demo",
            experiment_key="experiment",
            config_version=3,
            stream_key="events:raw:demo",
            marker_token="a" * 64,
            publish_attempts=attempts,
        )
        state = await writer._record_boundary_marker_failure(
            marker,
            writer_module.BoundaryMarkerPublishError(
                failure_code,
                terminal=terminal,
            ),
        )

        assert state == "quarantined"
        query, args = connection.call
        assert "marker_publish_attempts + 1" in query
        assert "clock_timestamp()" in query
        assert args[3] == attempts
        assert args[4] == failure_code
        assert args[5] is True
        assert args[6] == 0
        assert args[7] == 5
        assert args[8] is None

    asyncio.run(scenario())


def test_boundary_marker_poison_id_collision_quarantines_without_claiming(
    monkeypatch,
):
    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Connection:
        def __init__(self):
            self.update_candidates = []

        def transaction(self):
            return Transaction()

        async def fetchrow(self, query, *args):
            assert "UPDATE experiment_analysis_boundaries" in query
            candidate = args[8]
            self.update_candidates.append(candidate)
            if candidate is not None:
                collision = writer_module.asyncpg.UniqueViolationError(
                    "duplicate observed marker authority"
                )
                collision.constraint_name = (
                    writer_module.BOUNDARY_MARKER_OBSERVED_IDENTITY_CONSTRAINT
                )
                raise collision
            return {
                "marker_publish_state": "quarantined",
                "marker_publish_attempts": 1,
                "marker_publish_observed_stream_id": None,
            }

    class Acquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def __init__(self, connection):
            self.connection = connection

        def acquire(self):
            return Acquire(self.connection)

    async def scenario():
        connection = Connection()
        writer, _, _ = make_writer(
            monkeypatch,
            authority_pool=Pool(connection),
        )
        marker = writer_module.PendingBoundaryMarker(
            project_id="demo",
            experiment_key="colliding",
            config_version=1,
            stream_key="events:raw:demo",
            marker_token="a" * 64,
            publish_attempts=0,
        )

        state = await writer._record_boundary_marker_failure(
            marker,
            writer_module.BoundaryMarkerPublishError(
                "invalid_boundary_marker_dedup",
                terminal=True,
                observed_stream_id="123-4",
            ),
        )

        assert state == "quarantined"
        assert connection.update_candidates == ["123-4", None]

        post_xadd_marker = writer_module.PendingBoundaryMarker(
            project_id="demo",
            experiment_key="post_xadd",
            config_version=1,
            stream_key="events:raw:demo",
            marker_token="b" * 64,
            publish_attempts=4,
        )
        with pytest.raises(writer_module.asyncpg.UniqueViolationError):
            await writer._record_boundary_marker_failure(
                post_xadd_marker,
                writer_module.BoundaryMarkerPublishError(
                    "boundary_authority_update_failed",
                    terminal=False,
                    observed_stream_id="456-7",
                ),
            )
        assert connection.update_candidates == ["123-4", None, "456-7"]

    asyncio.run(scenario())


def test_boundary_marker_malformed_authority_is_classified_before_redis(
    monkeypatch,
):
    async def scenario():
        writer, _, _ = make_writer(monkeypatch)
        marker = writer_module.PendingBoundaryMarker(
            project_id="demo",
            experiment_key="experiment",
            config_version=3,
            stream_key="events:raw:other",
            marker_token="a" * 64,
            publish_attempts=0,
        )

        with pytest.raises(
            writer_module.BoundaryMarkerPublishError,
            match="invalid_stream_authority",
        ) as exc_info:
            await writer._publish_one_boundary_marker(marker)

        assert exc_info.value.terminal is True

    asyncio.run(scenario())


def test_boundary_marker_dedup_poison_is_terminal_and_expected_id_is_bound(
    monkeypatch,
):
    class DedupPoisonRedis(FakeRedis):
        def __init__(self):
            super().__init__()
            self.eval_args = None

        async def eval(self, *args):
            self.eval_args = args
            return [
                writer_module.BOUNDARY_MARKER_DEDUP_INVALID_REPLY,
                "123-4",
            ]

    async def scenario():
        redis_client = DedupPoisonRedis()
        writer, _, _ = make_writer(
            monkeypatch,
            redis_client=redis_client,
        )
        marker = writer_module.PendingBoundaryMarker(
            project_id="demo",
            experiment_key="experiment",
            config_version=3,
            stream_key="events:raw:demo",
            marker_token="a" * 64,
            publish_attempts=1,
            observed_stream_id="123-4",
        )

        with pytest.raises(
            writer_module.BoundaryMarkerPublishError,
            match="invalid_boundary_marker_dedup",
        ) as exc_info:
            await writer._publish_one_boundary_marker(marker)

        assert exc_info.value.terminal is True
        assert exc_info.value.observed_stream_id == "123-4"
        assert redis_client.eval_args[-1] == "123-4"
        assert "XRANGE" in writer_module.BOUNDARY_MARKER_LUA
        assert "observed_kind ~= ARGV[2]" in writer_module.BOUNDARY_MARKER_LUA
        assert "observed_token ~= ARGV[3]" in writer_module.BOUNDARY_MARKER_LUA

    asyncio.run(scenario())


def test_quarantined_observed_marker_degrades_before_becoming_ackable(
    monkeypatch,
):
    token = "a" * 64

    class Connection:
        async def fetch(
            self,
            query,
            project_id,
            stream_key,
            tokens,
            delivery_ids,
        ):
            assert "marker_publish_observed_stream_id" in query
            assert project_id == "demo"
            assert stream_key == "events:raw:demo"
            assert tokens == [token]
            assert delivery_ids == ["123-4"]
            return [
                {
                    "marker_token": token,
                    "marker_stream_id": None,
                    "marker_publish_state": "quarantined",
                    "marker_publish_observed_stream_id": "123-4",
                }
            ]

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    async def scenario():
        writer, _, _ = make_writer(
            monkeypatch,
            authority_pool=Pool(),
        )
        degraded = []

        async def degrade(stream_key, failure_reason):
            degraded.append((stream_key, failure_reason))

        monkeypatch.setattr(writer, "_degrade_pipeline_authority", degrade)
        writer._boundary_tokens_by_delivery[
            ("events:raw:demo", "123-4")
        ] = token

        await writer._verify_boundary_deliveries(
            "events:raw:demo",
            ["123-4"],
        )

        assert degraded == [
            ("events:raw:demo", "stream_state_unverifiable")
        ]

    asyncio.run(scenario())


def test_durable_ack_lock_contention_isolated_per_stream(monkeypatch):
    async def scenario():
        writer, _, _ = make_writer(monkeypatch)
        blocked_started = asyncio.Event()
        release_blocked = asyncio.Event()
        healthy_finished = asyncio.Event()

        async def finalize(stream_key, message_ids):
            assert message_ids == ["1-0"]
            if stream_key == "events:raw:blocked":
                blocked_started.set()
                await release_blocked.wait()
                raise TimeoutError("PostgreSQL row lock timed out")
            assert stream_key == "events:raw:healthy"
            healthy_finished.set()

        monkeypatch.setattr(writer, "_finalize_durable_stream", finalize)
        writer._queue_durable_ack(
            [
                BufferedEvent(
                    stream_key="events:raw:blocked",
                    message_id="1-0",
                    row={},
                ),
                BufferedEvent(
                    stream_key="events:raw:healthy",
                    message_id="1-0",
                    row={},
                ),
            ]
        )

        ack_task = asyncio.create_task(writer._ack_durable_messages())
        await blocked_started.wait()
        await asyncio.wait_for(healthy_finished.wait(), timeout=0.1)
        assert ack_task.done() is False
        release_blocked.set()

        assert await ack_task is False
        assert writer._durable_pending_ack == {
            "events:raw:blocked": ["1-0"]
        }
        assert writer._delivery_is_backpressured() is False
        assert writer._delivery_is_backpressured(
            "events:raw:blocked"
        ) is True
        assert writer._delivery_is_backpressured(
            "events:raw:healthy"
        ) is False

    asyncio.run(scenario())


def test_ack_advances_authority_only_after_clickhouse_durable_redis_ack(monkeypatch):
    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Connection:
        def __init__(self):
            self.updates = []

        def transaction(self):
            return Transaction()

        async def fetchrow(self, query, *_args):
            assert "FROM event_pipeline_watermarks" in query
            return {
                "stream_key": "events:raw:demo",
                "provenance_start_stream_id": "0-0",
                "contiguous_stream_id": "1-0",
                "consumer_group_entries_read": 0,
                "status": "healthy",
            }

        async def execute(self, query, *args):
            self.updates.append((query, args))
            return "UPDATE 1"

    class Acquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def __init__(self, connection):
            self.connection = connection

        def acquire(self):
            return Acquire(self.connection)

    async def scenario():
        connection = Connection()
        writer, redis_client, _ = make_writer(
            monkeypatch,
            authority_pool=Pool(connection),
        )
        redis_client.stream_groups["events:raw:demo"] = [
            {
                "name": "clickhouse-writer",
                "pending": 0,
                "lag": 0,
                "last-delivered-id": "2-3",
                "entries-read": 1,
            }
        ]
        writer._queue_durable_ack([buffered_event("2-3")])

        assert await writer._ack_durable_messages() is True

        frontier_updates = [
            args
            for query, args in connection.updates
            if "SET contiguous_stream_id" in query
        ]
        assert frontier_updates == [("demo", "2-3", 1)]
        assert redis_client.acks == [("events:raw:demo", "clickhouse-writer", ("2-3",))]

    asyncio.run(scenario())


def test_ack_fails_closed_when_entries_read_crosses_unfinalized_delivery(monkeypatch):
    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Connection:
        def __init__(self):
            self.status = "healthy"

        def transaction(self):
            return Transaction()

        async def fetchrow(self, query, *_args):
            assert "FROM event_pipeline_watermarks" in query
            return {
                "stream_key": "events:raw:demo",
                "provenance_start_stream_id": "0-0",
                "contiguous_stream_id": "1-0",
                "consumer_group_entries_read": 0,
                "status": self.status,
            }

        async def execute(self, query, *_args):
            if "failure_reason = 'stream_state_unverifiable'" not in query:
                raise AssertionError(query)
            self.status = "degraded"
            return "UPDATE 1"

    class Acquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def __init__(self, connection):
            self.connection = connection

        def acquire(self):
            return Acquire(self.connection)

    async def scenario():
        connection = Connection()
        writer, redis_client, _ = make_writer(
            monkeypatch,
            authority_pool=Pool(connection),
        )
        redis_client.stream_groups["events:raw:demo"] = [
            {
                "name": "clickhouse-writer",
                "pending": 0,
                "lag": 0,
                "last-delivered-id": "2-0",
                "entries-read": 2,
            }
        ]
        writer._queue_durable_ack([buffered_event("2-0")])

        assert await writer._ack_durable_messages() is True
        assert connection.status == "degraded"

    asyncio.run(scenario())


def test_first_finalize_requires_exact_ack_and_delete_counts(monkeypatch):
    class InexactPipeline(FakePipeline):
        async def execute(self):
            return [0, 0]

    class ResetRedis(FakeRedis):
        def pipeline(self, *, transaction):
            return InexactPipeline(self, transaction=transaction)

    async def scenario():
        writer, _, _ = make_writer(monkeypatch, redis_client=ResetRedis())
        writer._queue_durable_ack([buffered_event("2-0")])

        assert await writer._ack_durable_messages() is False
        assert writer._durable_pending_ack == {"events:raw:demo": ["2-0"]}

    asyncio.run(scenario())


def test_unknown_finalize_outcome_accepts_only_idempotent_zero_retry(monkeypatch):
    class UnknownThenZeroPipeline(FakePipeline):
        async def execute(self):
            self.redis_client.finalize_attempts += 1
            if self.redis_client.finalize_attempts == 1:
                raise ConnectionError("reply lost after EXEC")
            return [0, 0]

    class UnknownThenZeroRedis(FakeRedis):
        def __init__(self):
            super().__init__()
            self.finalize_attempts = 0

        def pipeline(self, *, transaction):
            return UnknownThenZeroPipeline(self, transaction=transaction)

    async def scenario():
        writer, _, _ = make_writer(
            monkeypatch,
            redis_client=UnknownThenZeroRedis(),
        )
        writer._queue_durable_ack([buffered_event("2-0")])

        assert await writer._ack_durable_messages() is False
        assert await writer._ack_durable_messages() is True
        assert writer._durable_pending_ack == {}
        assert writer._uncertain_redis_finalizations == set()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "stream_groups",
    [
        [],
        [
            {
                "name": "clickhouse-writer",
                "pending": 0,
                "lag": 0,
                "last-delivered-id": "4-0",
                "entries-read": 4,
            }
        ],
        [
            {
                "name": "clickhouse-writer",
                "pending": 0,
                "lag": 0,
                "last-delivered-id": "6-0",
                "entries-read": 6,
            }
        ],
        [
            {
                "name": "clickhouse-writer",
                "pending": 0,
                "lag": 0,
                "last-delivered-id": "5-0",
                "entries-read": 6,
            }
        ],
    ],
)
def test_redis_group_loss_or_rollback_permanently_degrades_authority(
    monkeypatch,
    stream_groups,
):
    class Connection:
        def __init__(self):
            self.watermark = {
                "stream_key": "events:raw:demo",
                "contiguous_stream_id": "5-0",
                "consumer_group_entries_read": 5,
                "status": "healthy",
                "failure_reason": None,
            }

        async def fetchrow(self, query, *_args):
            if "FROM event_pipeline_watermarks" not in query:
                raise AssertionError(query)
            return dict(self.watermark)

        async def execute(self, query, *_args):
            if "status = 'degraded'" not in query:
                raise AssertionError(query)
            self.watermark["status"] = "degraded"
            self.watermark["failure_reason"] = "stream_state_unverifiable"
            return "INSERT 0 1"

    class Acquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def __init__(self, connection):
            self.connection = connection

        def acquire(self):
            return Acquire(self.connection)

    async def scenario():
        connection = Connection()
        writer, redis_client, _ = make_writer(
            monkeypatch,
            authority_pool=Pool(connection),
        )
        redis_client.stream_groups["events:raw:demo"] = stream_groups

        await writer._ensure_consumer_group("events:raw:demo")

        assert connection.watermark["status"] == "degraded"
        assert connection.watermark["failure_reason"] == "stream_state_unverifiable"

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


def test_server_timestamp_is_required_at_the_writer_boundary(monkeypatch):
    writer, _, _ = make_writer(monkeypatch)
    payload = canonical_event()
    payload.pop("server_timestamp")

    with pytest.raises(
        ValueError,
        match="missing required fields: server_timestamp",
    ):
        writer._parse_event({"event_json": json.dumps(payload)}, "demo")


@pytest.mark.parametrize(
    ("timestamp", "message"),
    [
        ("2026-07-06T11:59:59.999999Z", "more than 7 days older"),
        ("2026-07-13T12:05:00.000001Z", "more than 5 minutes ahead"),
    ],
)
def test_out_of_window_timestamps_are_rejected_at_the_writer_boundary(
    monkeypatch,
    timestamp,
    message,
):
    writer, _, _ = make_writer(monkeypatch)
    payload = canonical_event(timestamp=timestamp)

    with pytest.raises(ValueError, match=message):
        writer._parse_event({"event_json": json.dumps(payload)}, "demo")


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-07-06T12:00:00.000Z",
        "2026-07-13T12:05:00.000Z",
    ],
)
def test_event_timestamp_window_boundaries_are_accepted(monkeypatch, timestamp):
    writer, _, _ = make_writer(monkeypatch)
    payload = canonical_event(timestamp=timestamp)

    assert writer._parse_event(
        {"event_json": json.dumps(payload)},
        "demo",
    )["timestamp"] == datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


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
        def execute(self, _query, *, external_tables=None, **_kwargs):
            rows = external_event_rows(external_tables)
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
        def execute(self, _query, *, external_tables=None, **_kwargs):
            rows = external_event_rows(external_tables)
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


def test_consume_skips_only_stream_with_unresolved_durable_ack(monkeypatch):
    class StoppingRedis(FakeRedis):
        writer = None

        async def xreadgroup(self, **kwargs):
            result = await super().xreadgroup(**kwargs)
            self.writer.running = False
            return result

    async def scenario():
        redis_client = StoppingRedis()
        writer, _, _ = make_writer(monkeypatch, redis_client=redis_client)
        redis_client.writer = writer
        writer._queue_durable_ack(
            [
                BufferedEvent(
                    stream_key="events:raw:alpha",
                    message_id="1-0",
                    row={},
                )
            ]
        )
        writer.running = True

        await writer._consume_loop(["alpha", "beta"])

        assert [call["streams"] for call in redis_client.read_args] == [
            {"events:raw:beta": ">"}
        ]
        assert writer._durable_pending_ack == {
            "events:raw:alpha": ["1-0"]
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
