"""Fail-closed tests for experiment decision dependency readiness."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from app import main, readiness


class PostgresConnection:
    def __init__(
        self,
        *,
        ledger_exists: bool = True,
        migration_name: str | None = readiness.REQUIRED_POSTGRES_MIGRATION[1],
        columns: set[tuple[str, str]] | None = None,
        privileges_ready: bool = True,
    ) -> None:
        self.ledger_exists = ledger_exists
        self.migration_name = migration_name
        self.columns = (
            set(readiness.REQUIRED_POSTGRES_COLUMNS)
            if columns is None
            else columns
        )
        self.privileges_ready = privileges_ready

    async def fetchval(self, query: str, *args):
        if "to_regclass" in query:
            return self.ledger_exists
        if "FROM apdl_schema_migrations" in query:
            assert args == (readiness.REQUIRED_POSTGRES_MIGRATION[0],)
            return self.migration_name
        if "has_table_privilege" in query:
            return self.privileges_ready
        raise AssertionError(f"unexpected PostgreSQL readiness query: {query}")

    async def fetch(self, query: str, *args):
        assert "FROM information_schema.columns" in query
        assert set(args[0]) == {
            table for table, _ in readiness.REQUIRED_POSTGRES_COLUMNS
        }
        return [
            {"table_name": table, "column_name": column}
            for table, column in sorted(self.columns)
        ]


class Acquire:
    def __init__(self, connection: PostgresConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> PostgresConnection:
        return self.connection

    async def __aexit__(self, *_exc) -> bool:
        return False


class PostgresPool:
    def __init__(self, connection: PostgresConnection) -> None:
        self.connection = connection

    def acquire(self) -> Acquire:
        return Acquire(self.connection)


class ClickHouseClient:
    def __init__(
        self,
        *,
        migration_name: str | None = readiness.REQUIRED_CLICKHOUSE_MIGRATION[1],
        columns: set[tuple[str, str]] | None = None,
        engines: dict[str, str] | None = None,
    ) -> None:
        self.migration_name = migration_name
        self.columns = (
            set(readiness.REQUIRED_CLICKHOUSE_COLUMNS)
            if columns is None
            else columns
        )
        self.engines = (
            dict(readiness.REQUIRED_CLICKHOUSE_ENGINES)
            if engines is None
            else engines
        )

    async def execute(self, query: str, params: dict):
        if "FROM apdl_schema_migrations FINAL" in query:
            assert params == {
                "migration_version": readiness.REQUIRED_CLICKHOUSE_MIGRATION[0]
            }
            if self.migration_name is None:
                return []
            return [{"name": self.migration_name}]
        if "FROM system.columns" in query:
            return [
                {"table": table, "name": column}
                for table, column in sorted(self.columns)
            ]
        if "FROM system.tables" in query:
            return [
                {"name": table, "engine": engine}
                for table, engine in sorted(self.engines.items())
            ]
        raise AssertionError(f"unexpected ClickHouse readiness query: {query}")


@pytest.mark.asyncio
async def test_postgres_decision_schema_accepts_exact_capabilities():
    assert readiness.REQUIRED_POSTGRES_MIGRATION == (
        41,
        "041_boundary_marker_retry_quarantine.sql",
    )
    await readiness.assert_postgres_decision_schema(
        PostgresPool(PostgresConnection())
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connection",
    [
        PostgresConnection(ledger_exists=False),
        PostgresConnection(migration_name=None),
        PostgresConnection(
            columns=set(readiness.REQUIRED_POSTGRES_COLUMNS)
            - {("experiment_analysis_boundaries", "marker_publish_state")}
        ),
        PostgresConnection(privileges_ready=False),
    ],
)
async def test_postgres_decision_schema_rejects_incomplete_capability(connection):
    with pytest.raises(RuntimeError):
        await readiness.assert_postgres_decision_schema(PostgresPool(connection))


@pytest.mark.asyncio
async def test_clickhouse_decision_schema_accepts_exact_capabilities():
    await readiness.assert_clickhouse_decision_schema(ClickHouseClient())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client",
    [
        ClickHouseClient(migration_name=None),
        ClickHouseClient(
            columns=set(readiness.REQUIRED_CLICKHOUSE_COLUMNS)
            - {("experiment_event_deliveries", "source_stream_id")}
        ),
        ClickHouseClient(
            engines={
                **readiness.REQUIRED_CLICKHOUSE_ENGINES,
                "events": "MergeTree",
            }
        ),
    ],
)
async def test_clickhouse_decision_schema_rejects_incomplete_capability(client):
    with pytest.raises(RuntimeError):
        await readiness.assert_clickhouse_decision_schema(client)


@pytest.mark.asyncio
async def test_dependency_gate_checks_postgres_clickhouse_and_config(monkeypatch):
    clickhouse_probe = AsyncMock()
    postgres_probe = AsyncMock()
    config_probe = AsyncMock()
    monkeypatch.setattr(
        readiness,
        "assert_clickhouse_decision_schema",
        clickhouse_probe,
    )
    monkeypatch.setattr(
        readiness,
        "assert_postgres_decision_schema",
        postgres_probe,
    )
    monkeypatch.setattr(
        readiness,
        "assert_experiment_analysis_capability",
        config_probe,
    )
    clickhouse_client = object()
    postgres_pool = object()

    await readiness.assert_decision_dependencies_ready(
        clickhouse_client,
        postgres_pool,
    )

    clickhouse_probe.assert_awaited_once_with(clickhouse_client)
    postgres_probe.assert_awaited_once_with(postgres_pool)
    config_probe.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_dependency_gate_propagates_capability_failure(monkeypatch):
    monkeypatch.setattr(
        readiness,
        "assert_clickhouse_decision_schema",
        AsyncMock(),
    )
    monkeypatch.setattr(
        readiness,
        "assert_postgres_decision_schema",
        AsyncMock(side_effect=RuntimeError("schema unavailable")),
    )
    monkeypatch.setattr(
        readiness,
        "assert_experiment_analysis_capability",
        AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="schema unavailable"):
        await readiness.assert_decision_dependencies_ready(object(), object())


def test_startup_validates_dependencies_before_accepting_maintenance_monitor():
    source = inspect.getsource(main.lifespan)

    gate = "await assert_decision_dependencies_ready(client, auth_pool)"
    monitor = "await _start_maintenance_monitor("
    assert gate in source
    assert monitor in source
    assert source.index(gate) < source.index(monitor)
