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
async def test_rejects_database_without_segmented_publication_migration():
    with pytest.raises(RuntimeError, match="024_codegen_segmented_publication.sql"):
        await assert_schema_ready(FakeConn(migration_name="023_llm_governance.sql"))


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


def test_durable_effects_migration_defines_strict_changeset_idempotency():
    migration = (
        Path(__file__).parents[3]
        / "pipeline/postgres/migrations/022_agents_durable_effects.sql"
    ).read_text()

    assert "ADD COLUMN IF NOT EXISTS idempotency_key TEXT" in migration
    assert "ADD COLUMN IF NOT EXISTS idempotency_request_sha256 CHAR(64)" in migration
    assert "ALTER COLUMN idempotency_key SET NOT NULL" in migration
    assert "ALTER COLUMN idempotency_request_sha256 SET NOT NULL" in migration
    assert "codegen_changesets_idempotency_key_check" in migration
    assert "codegen_changesets_idempotency_request_sha256_check" in migration
    assert "'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$'" in migration
    assert "ON codegen_changesets (project_id, idempotency_key)" in migration
    assert "FOREIGN KEY (project_id, retry_of_changeset_id)" in migration
    assert "REFERENCES codegen_changesets(project_id, changeset_id)" in migration
    assert "'legacy:'\n        || md5(" in migration


def test_shutdown_awaits_requeued_jobs_before_closing_database():
    main_source = (Path(__file__).parents[1] / "app" / "main.py").read_text()
    await_requeued = "await asyncio.gather(*requeued_jobs, return_exceptions=True)"
    assert main_source.index(await_requeued) < main_source.index("await pool.close()")
