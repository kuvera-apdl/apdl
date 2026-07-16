"""Static contracts for Config's atomic-mutation migration."""

from pathlib import Path


ATOMIC_MIGRATION_SQL = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "postgres"
    / "migrations"
    / "012_config_atomic_mutations.sql"
).read_text()


def _table_definition(sql: str, table: str) -> str:
    start = sql.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    return sql[start : sql.index("\n);", start) + 3]


def test_experiment_version_and_backing_flag_ownership_are_database_invariants():
    sql = ATOMIC_MIGRATION_SQL

    assert "ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1" in sql
    assert "experiments_version_check CHECK (version >= 1)" in sql
    assert (
        "experiments_flag_key_unique UNIQUE (project_id, flag_key)" in sql
    )
    assert "FOREIGN KEY (project_id, flag_key)" in sql
    assert "REFERENCES flags (project_id, key)" in sql
    assert "ON UPDATE RESTRICT" in sql
    assert "ON DELETE RESTRICT" in sql
    assert "BEFORE UPDATE OF project_id, flag_key ON experiments" in sql
    assert "Experiment backing-flag ownership is immutable" in sql


def test_experiment_lifecycle_uses_timezone_aware_ordered_timestamps():
    sql = ATOMIC_MIGRATION_SQL

    assert "start_date !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'" in sql
    assert "end_date !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'" in sql
    assert "ALTER COLUMN start_date DROP DEFAULT" in sql
    assert "ALTER COLUMN end_date DROP DEFAULT" in sql
    assert "ALTER COLUMN start_date DROP NOT NULL" in sql
    assert "ALTER COLUMN end_date DROP NOT NULL" in sql
    assert "ALTER COLUMN start_date TYPE TIMESTAMPTZ" in sql
    assert "USING NULLIF(start_date, '')::TIMESTAMPTZ" in sql
    assert "ALTER COLUMN end_date TYPE TIMESTAMPTZ" in sql
    assert "USING NULLIF(end_date, '')::TIMESTAMPTZ" in sql
    assert (
        "status IN ('draft', 'scheduled', 'running', 'completed', 'stopped')"
        in sql
    )
    assert "experiments_date_window_check" in sql
    assert (
        "end_date IS NULL OR (start_date IS NOT NULL AND end_date > start_date)"
        in sql
    )


def test_exact_prefix_lifecycle_rows_are_rejected_instead_of_reinterpreted():
    sql = ATOMIC_MIGRATION_SQL
    lifecycle = sql[sql.index("DO $validate_experiment_lifecycle$") :]

    assert sql.index("ALTER COLUMN start_date TYPE TIMESTAMPTZ") < sql.index(
        "DO $validate_experiment_lifecycle$"
    )
    assert "status IN ('scheduled', 'running')" in lifecycle
    assert (
        "start_date IS NULL OR end_date IS NULL OR end_date <= start_date"
        in lifecycle
    )
    assert "status = 'scheduled'" in lifecycle
    assert "start_date <= now()" in lifecycle
    assert "status = 'running'" in lifecycle
    assert "start_date > now() OR end_date <= now()" in lifecycle
    assert "jsonb_typeof(primary_metric_json::jsonb)" in lifecycle
    assert "primary_metric_json::jsonb ? 'event'" in lifecycle
    assert "scheduled/running rows require a primary metric event" in lifecycle
    assert "UPDATE experiments" not in lifecycle
    assert "DELETE FROM experiments" not in lifecycle


def test_ownership_migration_aborts_instead_of_guessing_at_inconsistent_rows():
    sql = ATOMIC_MIGRATION_SQL

    assert "LEFT JOIN flags AS flag" in sql
    assert "WHERE flag.key IS NULL" in sql
    assert "GROUP BY project_id, flag_key" in sql
    assert "HAVING count(*) > 1" in sql
    assert "UPDATE experiments" not in sql
    assert "DELETE FROM experiments" not in sql


def test_audit_origin_is_typed_separately_from_authenticated_actor():
    sql = ATOMIC_MIGRATION_SQL

    assert "ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'manual'" in sql
    assert "flag_audit_log_origin_check" in sql
    assert (
        "origin IN ('manual', 'automation', 'experiment', 'scheduler')" in sql
    )


def test_config_outbox_has_durable_retry_and_tenant_scoped_deduplication():
    outbox = _table_definition(ATOMIC_MIGRATION_SQL, "config_outbox")

    assert "id BIGSERIAL PRIMARY KEY" in outbox
    assert "project_id TEXT NOT NULL" in outbox
    assert "kind IN ('flag_change', 'experiment_change', 'exposure')" in outbox
    assert "dedup_key TEXT NOT NULL" in outbox
    assert "payload JSONB NOT NULL" in outbox
    assert "attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0)" in outbox
    assert "available_at TIMESTAMPTZ NOT NULL DEFAULT now()" in outbox
    assert "claimed_at TIMESTAMPTZ" in outbox
    assert "processed_at TIMESTAMPTZ" in outbox
    assert "last_error TEXT NOT NULL DEFAULT ''" in outbox
    assert "created_at TIMESTAMPTZ NOT NULL DEFAULT now()" in outbox
    assert "UNIQUE (project_id, kind, dedup_key)" in outbox


def test_config_outbox_pending_scan_is_partial_and_ordered():
    sql = ATOMIC_MIGRATION_SQL

    assert "CREATE INDEX IF NOT EXISTS idx_config_outbox_pending" in sql
    assert "ON config_outbox (available_at, id)" in sql
    assert "WHERE processed_at IS NULL" in sql
