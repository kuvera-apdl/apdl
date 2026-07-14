"""Tests for fail-fast Codegen schema validation."""

from pathlib import Path

import pytest

from app.db import MIGRATION_NAME, REQUIRED_COLUMNS, assert_schema_ready


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
async def test_rejects_missing_migration_ledger():
    with pytest.raises(RuntimeError, match="migration ledger is missing"):
        await assert_schema_ready(FakeConn(ledger_exists=False))


@pytest.mark.asyncio
async def test_rejects_database_without_publication_identity_migration():
    with pytest.raises(RuntimeError, match="010_codegen_publication_identity.sql"):
        await assert_schema_ready(
            FakeConn(migration_name="009_codegen_repository_authority.sql")
        )


@pytest.mark.asyncio
async def test_rejects_incomplete_schema_at_startup():
    columns = REQUIRED_COLUMNS - {("github_repository_grants", "repository_id")}
    with pytest.raises(RuntimeError, match="github_repository_grants.repository_id"):
        await assert_schema_ready(FakeConn(columns=columns))


def test_codegen_startup_contains_no_postgres_ddl():
    app_dir = Path(__file__).parents[1] / "app"
    main_source = (app_dir / "main.py").read_text()
    db_source = (app_dir / "db.py").read_text()
    assert "CREATE TABLE" not in main_source
    assert "ALTER TABLE" not in main_source
    assert "CREATE TABLE" not in db_source
    assert "ALTER TABLE" not in db_source


def test_shutdown_awaits_requeued_jobs_before_closing_database():
    main_source = (Path(__file__).parents[1] / "app" / "main.py").read_text()
    await_requeued = "await asyncio.gather(*requeued_jobs, return_exceptions=True)"
    assert main_source.index(await_requeued) < main_source.index("await pool.close()")
