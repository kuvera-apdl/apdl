"""Tests for fail-fast Agents schema validation."""

from pathlib import Path

import pytest

from app.memory.embeddings import EMBEDDING_DIMENSIONS
from app.schema import (
    MIGRATION_NAME,
    MIGRATION_VERSION,
    REQUIRED_COLUMNS,
    assert_schema_ready,
)


class FakeConn:
    def __init__(
        self,
        *,
        ledger_exists: bool = True,
        migration_name: str | None = MIGRATION_NAME,
        columns=REQUIRED_COLUMNS,
        dimension: int = EMBEDDING_DIMENSIONS,
    ):
        self.ledger_exists = ledger_exists
        self.migration_name = migration_name
        self.columns = set(columns)
        self.dimension = dimension

    async def fetchval(self, sql: str, *args):
        if "to_regclass" in sql:
            return self.ledger_exists
        if "apdl_schema_migrations" in sql:
            return self.migration_name
        if "atttypmod" in sql:
            return self.dimension
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


def test_startup_requires_current_agents_contract_migration():
    assert MIGRATION_VERSION == 23
    assert MIGRATION_NAME == "023_llm_governance.sql"
    assert ("admin_projects", "created_by") in REQUIRED_COLUMNS
    assert ("feature_proposals", "project_id") in REQUIRED_COLUMNS
    assert ("custom_agent_test_runs", "lease_expires_at") in REQUIRED_COLUMNS
    assert (
        "agent_mutation_quota_reservations",
        "idempotency_key",
    ) in REQUIRED_COLUMNS
    assert ("agent_run_results", "metadata") in REQUIRED_COLUMNS
    assert ("agent_approval_effects", "lease_expires_at") in REQUIRED_COLUMNS
    assert ("llm_project_policies", "required_data_residency") in REQUIRED_COLUMNS
    assert ("llm_provider_attempts", "egress_started_at") in REQUIRED_COLUMNS


@pytest.mark.asyncio
async def test_rejects_missing_migration_ledger():
    with pytest.raises(RuntimeError, match="migration ledger is missing"):
        await assert_schema_ready(FakeConn(ledger_exists=False))


@pytest.mark.asyncio
async def test_rejects_incomplete_schema_at_startup():
    columns = REQUIRED_COLUMNS - {("agent_runs", "status")}
    with pytest.raises(RuntimeError, match="agent_runs.status"):
        await assert_schema_ready(FakeConn(columns=columns))


@pytest.mark.asyncio
async def test_rejects_missing_project_provenance_column_at_startup():
    columns = REQUIRED_COLUMNS - {("admin_projects", "created_by")}
    with pytest.raises(RuntimeError, match="admin_projects.created_by"):
        await assert_schema_ready(FakeConn(columns=columns))


@pytest.mark.asyncio
async def test_rejects_vector_dimension_drift_without_mutating_rows():
    with pytest.raises(RuntimeError, match="explicit migration"):
        await assert_schema_ready(FakeConn(dimension=1536))


def test_agents_startup_contains_no_postgres_ddl():
    main_source = (Path(__file__).parents[1] / "app" / "main.py").read_text()
    assert "CREATE TABLE" not in main_source
    assert "ALTER TABLE" not in main_source
    assert "CREATE EXTENSION" not in main_source
