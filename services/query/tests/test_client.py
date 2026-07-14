"""Tests for ClickHouse client query preparation and hard budgets."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from asynch.errors import ErrorCode, ServerException
from asynch.proto.connection import Connection

from app.clickhouse.client import (
    ClickHouseClient,
    QueryBudgetExceeded,
    QueryConcurrencyExceeded,
    normalize_query_params,
)


class FakeCursor:
    def __init__(self, *, gate: asyncio.Event | None = None):
        self.gate = gate
        self.settings = None
        self.query = ""
        self.params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def set_settings(self, settings):
        self.settings = settings

    async def execute(self, query, params):
        self.query = query
        self.params = params
        if self.gate is not None:
            await self.gate.wait()

    async def fetchall(self):
        return [{"value": 1}]


class FakeConnection:
    def __init__(self, cursor: FakeCursor):
        self.fake_cursor = cursor
        self.closed = False

    def cursor(self, **_):
        return self.fake_cursor

    async def close(self):
        self.closed = True


def test_normalize_query_params_uses_asynch_placeholder_style():
    query = """
SELECT *
FROM events
WHERE project_id = %(project_id)s
  AND event_date BETWEEN %(start_date)s AND %(end_date)s
  AND event_name IN (%(ev_0)s, %(ev_1)s)
"""

    normalized = normalize_query_params(query)

    assert "%(project_id)s" not in normalized
    assert "project_id = {project_id}" in normalized
    assert "event_name IN ({ev_0}, {ev_1})" in normalized


def test_normalized_query_can_be_substituted_by_asynch():
    query = normalize_query_params(
        "SELECT * FROM events WHERE project_id = %(project_id)s "
        "AND event_name = %(event_name)s"
    )

    compiled = Connection.substitute_params(
        query,
        {
            "project_id": "demo",
            "event_name": "$click",
        },
    )

    assert "project_id = 'demo'" in compiled
    assert "event_name = '$click'" in compiled


@pytest.mark.asyncio
async def test_execute_applies_clickhouse_resource_settings():
    client = ClickHouseClient()
    cursor = FakeCursor()
    client._pool = [FakeConnection(cursor)]

    rows = await client.execute(
        "SELECT %(value)s AS value WHERE project_id = %(project_id)s",
        {"value": 1, "project_id": "demo"},
    )

    assert rows == [{"value": 1}]
    assert cursor.settings == client._settings
    assert cursor.settings["max_execution_time"] <= 30
    assert cursor.settings["max_rows_to_read"] <= 20_000_000
    assert cursor.settings["max_bytes_to_read"] <= 1_073_741_824
    assert cursor.settings["max_result_rows"] <= 100_000
    assert cursor.settings["max_result_bytes"] <= 67_108_864
    assert cursor.settings["max_memory_usage"] <= 1_073_741_824
    assert cursor.settings["max_threads"] <= 8
    assert cursor.settings["read_overflow_mode"] == "throw"
    assert cursor.settings["result_overflow_mode"] == "throw"


@pytest.mark.asyncio
async def test_timeout_discards_connection():
    client = ClickHouseClient()
    client._timeout_seconds = 0.01
    conn = FakeConnection(FakeCursor(gate=asyncio.Event()))
    client._pool = [conn]

    with pytest.raises(QueryBudgetExceeded, match="execution budget"):
        await client.execute("SELECT 1", {"project_id": "demo"})

    assert conn.closed is True
    assert client._inflight_by_project == {}


@pytest.mark.asyncio
async def test_cancellation_discards_connection_and_releases_all_budgets():
    client = ClickHouseClient()
    gate = asyncio.Event()
    conn = FakeConnection(FakeCursor(gate=gate))
    client._pool = [conn]

    task = asyncio.create_task(
        client.execute("SELECT 1", {"project_id": "demo"})
    )
    while client._inflight_total == 0:
        await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert conn.closed is True
    assert client._pool == []
    assert client._inflight_by_project == {}
    assert client._inflight_total == 0


@pytest.mark.asyncio
async def test_project_concurrency_limit_fails_fast():
    client = ClickHouseClient()
    client._project_limit = 1
    gate = asyncio.Event()
    client._pool = [FakeConnection(FakeCursor(gate=gate))]

    first = asyncio.create_task(
        client.execute("SELECT 1", {"project_id": "demo"})
    )
    await asyncio.sleep(0)
    with pytest.raises(QueryConcurrencyExceeded, match="active queries"):
        await client.execute("SELECT 2", {"project_id": "demo"})

    gate.set()
    assert await first == [{"value": 1}]
    assert client._inflight_by_project == {}
    assert client._inflight_total == 0


@pytest.mark.asyncio
async def test_global_connection_budget_fails_fast_across_projects():
    client = ClickHouseClient()
    client._pool_size = 1
    gate = asyncio.Event()
    client._pool = [FakeConnection(FakeCursor(gate=gate))]

    first = asyncio.create_task(
        client.execute("SELECT 1", {"project_id": "project-a"})
    )
    await asyncio.sleep(0)
    with pytest.raises(QueryConcurrencyExceeded, match="global active-query"):
        await client.execute("SELECT 2", {"project_id": "project-b"})

    gate.set()
    assert await first == [{"value": 1}]
    assert client._inflight_total == 0


@pytest.mark.asyncio
async def test_clickhouse_budget_rejection_is_typed_and_discards_connection():
    cursor = FakeCursor()
    cursor.execute = AsyncMock(
        side_effect=ServerException(
            "Memory limit exceeded",
            ErrorCode.MEMORY_LIMIT_EXCEEDED,
        )
    )
    conn = FakeConnection(cursor)
    client = ClickHouseClient()
    client._pool = [conn]

    with pytest.raises(QueryBudgetExceeded, match="resource budget"):
        await client.execute("SELECT 1", {"project_id": "demo"})

    assert conn.closed is True
    assert client._inflight_total == 0
