"""Tests for fail-fast Config schema validation."""

from pathlib import Path

import pytest

from app.schema import MIGRATION_NAME, REQUIRED_COLUMNS, assert_schema_ready


class FakeConn:
    def __init__(
        self,
        *,
        ledger_exists: bool = True,
        migration_name: str | None = MIGRATION_NAME,
        columns=REQUIRED_COLUMNS,
    ):
        self.ledger_exists = ledger_exists
        self.migration_name = migration_name
        self.columns = set(columns)

    async def fetchval(self, sql: str, *args):
        if "to_regclass" in sql:
            return self.ledger_exists
        if "apdl_schema_migrations" in sql:
            return self.migration_name
        raise AssertionError(sql)

    async def fetch(self, sql: str, *args):
        assert "information_schema.columns" in sql
        return [
            {"table_name": table, "column_name": column}
            for table, column in self.columns
        ]


@pytest.mark.asyncio
async def test_accepts_complete_migrated_schema():
    await assert_schema_ready(FakeConn())


@pytest.mark.asyncio
async def test_rejects_missing_required_migration():
    with pytest.raises(RuntimeError, match=MIGRATION_NAME):
        await assert_schema_ready(FakeConn(migration_name=None))


@pytest.mark.asyncio
async def test_rejects_incomplete_schema_at_startup():
    columns = REQUIRED_COLUMNS - {("flags", "fallthrough")}
    with pytest.raises(RuntimeError, match="flags.fallthrough"):
        await assert_schema_ready(FakeConn(columns=columns))


def test_config_startup_executes_no_postgres_ddl():
    main_source = (Path(__file__).parents[1] / "app" / "main.py").read_text()
    startup = main_source[main_source.index("async def lifespan") :]
    assert "conn.execute(CREATE_" not in startup
    assert "conn.execute(MIGRATE_" not in startup
    assert "await assert_schema_ready(conn)" in startup
