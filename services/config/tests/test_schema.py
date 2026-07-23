"""Tests for fail-fast Config schema validation."""

from pathlib import Path

import pytest

from app.schema import (
    MIGRATION_NAME,
    MIGRATION_VERSION,
    REQUIRED_COLUMNS,
    REQUIRED_CONSTRAINTS,
    assert_schema_ready,
)


class FakeConn:
    def __init__(
        self,
        *,
        ledger_exists: bool = True,
        migration_name: str | None = MIGRATION_NAME,
        columns=REQUIRED_COLUMNS,
        constraints=REQUIRED_CONSTRAINTS,
        unvalidated_constraints=frozenset(),
    ):
        self.ledger_exists = ledger_exists
        self.migration_name = migration_name
        self.columns = set(columns)
        self.constraints = set(constraints)
        self.unvalidated_constraints = set(unvalidated_constraints)
        self.migration_version = None

    async def fetchval(self, sql: str, *args):
        if "to_regclass" in sql:
            return self.ledger_exists
        if "apdl_schema_migrations" in sql:
            self.migration_version = args[0]
            return self.migration_name
        raise AssertionError(sql)

    async def fetch(self, sql: str, *args):
        if "information_schema.columns" in sql:
            return [
                {"table_name": table, "column_name": column}
                for table, column in self.columns
            ]
        if "pg_catalog.pg_constraint" in sql:
            return [
                {
                    "table_name": table,
                    "constraint_name": constraint,
                    "constraint_validated": (
                        (table, constraint)
                        not in self.unvalidated_constraints
                    ),
                }
                for table, constraint in self.constraints
            ]
        raise AssertionError(sql)


@pytest.mark.asyncio
async def test_accepts_complete_migrated_schema():
    conn = FakeConn()

    await assert_schema_ready(conn)

    assert conn.migration_version == MIGRATION_VERSION


@pytest.mark.asyncio
async def test_rejects_missing_required_migration():
    with pytest.raises(RuntimeError, match=MIGRATION_NAME):
        await assert_schema_ready(FakeConn(migration_name=None))


@pytest.mark.asyncio
async def test_rejects_previous_config_migration_as_not_release_ready():
    with pytest.raises(RuntimeError, match=MIGRATION_NAME):
        await assert_schema_ready(
            FakeConn(migration_name="012_config_atomic_mutations.sql")
        )


@pytest.mark.asyncio
async def test_rejects_incomplete_schema_at_startup():
    columns = REQUIRED_COLUMNS - {("flags", "fallthrough")}
    with pytest.raises(RuntimeError, match="flags.fallthrough"):
        await assert_schema_ready(FakeConn(columns=columns))


@pytest.mark.asyncio
async def test_rejects_missing_atomic_mutation_columns():
    columns = REQUIRED_COLUMNS - {
        ("experiments", "version"),
        ("flag_audit_log", "origin"),
        ("config_outbox", "dedup_key"),
    }

    with pytest.raises(
        RuntimeError,
        match=(
            "config_outbox.dedup_key, experiments.version, "
            "flag_audit_log.origin"
        ),
    ):
        await assert_schema_ready(FakeConn(columns=columns))


@pytest.mark.asyncio
async def test_rejects_missing_variant_weight_constraint():
    constraints = REQUIRED_CONSTRAINTS - {
        ("experiments", "experiments_variants_canonical_check")
    }
    with pytest.raises(
        RuntimeError,
        match="experiments.experiments_variants_canonical_check",
    ):
        await assert_schema_ready(FakeConn(constraints=constraints))


@pytest.mark.asyncio
async def test_rejects_missing_experiment_bucket_constraint():
    constraints = REQUIRED_CONSTRAINTS - {
        ("experiments", "experiments_bucket_by_check")
    }
    with pytest.raises(
        RuntimeError,
        match="experiments.experiments_bucket_by_check",
    ):
        await assert_schema_ready(FakeConn(constraints=constraints))


@pytest.mark.asyncio
async def test_rejects_unvalidated_variant_weight_constraint():
    unvalidated = {
        ("flags", "flags_variants_canonical_check"),
    }
    with pytest.raises(
        RuntimeError,
        match="flags.flags_variants_canonical_check",
    ):
        await assert_schema_ready(
            FakeConn(unvalidated_constraints=unvalidated)
        )


def test_config_startup_executes_no_postgres_ddl():
    main_source = (Path(__file__).parents[1] / "app" / "main.py").read_text()
    startup = main_source[main_source.index("async def lifespan") :]
    assert "conn.execute(CREATE_" not in startup
    assert "conn.execute(MIGRATE_" not in startup
    assert "await assert_schema_ready(lock_conn)" in startup
