"""Contract tests for ClickHouse analytics migrations."""

from pathlib import Path


MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[3] / "pipeline" / "clickhouse" / "migrations"
)
ROOT = Path(__file__).resolve().parents[3]
BACKFILLS_DIR = ROOT / "pipeline" / "clickhouse" / "backfills"
FEATURE_FLAG_EXPOSURES_SQL = (
    MIGRATIONS_DIR / "006_feature_flag_exposures.sql"
).read_text()
EVENTS_SQL = (MIGRATIONS_DIR / "001_events.sql").read_text()
FRONTEND_HEALTH_EVENTS_SQL = (
    MIGRATIONS_DIR / "007_frontend_health_events.sql"
).read_text()
LEGACY_EXPERIMENTS_SQL = (MIGRATIONS_DIR / "003_experiments.sql").read_text()
MATERIALIZED_VIEWS_SQL = (MIGRATIONS_DIR / "004_materialized_views.sql").read_text()
IDENTITY_ALIASES_SQL = (MIGRATIONS_DIR / "011_identity_aliases.sql").read_text()
PROTOTYPE_RETIREMENT_SQL = (
    MIGRATIONS_DIR / "012_retire_prototype_schemas.sql"
).read_text()
IDENTITY_ALIASES_BACKFILL_SQL = (
    BACKFILLS_DIR / "011_identity_aliases.sql"
).read_text()
CLICKHOUSE_INIT_SCRIPT = (ROOT / "scripts" / "init-clickhouse.sh").read_text()
CLICKHOUSE_MIGRATION_ENGINE = (
    ROOT / "pipeline" / "clickhouse" / "migrate.py"
).read_text()
EVENTS_UPGRADE_SQL = (MIGRATIONS_DIR / "005_events_canonical_upgrade.sql").read_text()


def test_events_table_replaces_retries_by_project_and_message_id():
    assert "ENGINE = ReplacingMergeTree(received_at)" in EVENTS_SQL
    assert "ORDER BY (project_id, message_id)" in EVENTS_SQL
    assert "ENGINE = MergeTree()" not in EVENTS_SQL


def test_pre_ledger_events_upgrade_rebuilds_engine_and_preserves_rows():
    assert "ALTER TABLE events ADD COLUMN IF NOT EXISTS message_id" in (
        EVENTS_UPGRADE_SQL
    )
    assert "DEFAULT toString(event_id)" in EVENTS_UPGRADE_SQL
    assert "ALTER TABLE events ADD COLUMN IF NOT EXISTS received_at" in (
        EVENTS_UPGRADE_SQL
    )
    assert "CREATE TABLE events__apdl_migration_005" in EVENTS_UPGRADE_SQL
    assert "ENGINE = ReplacingMergeTree(received_at)" in EVENTS_UPGRADE_SQL
    assert "ORDER BY (project_id, message_id)" in EVENTS_UPGRADE_SQL
    assert "FROM events;" in EVENTS_UPGRADE_SQL
    assert "EXCHANGE TABLES events AND events__apdl_migration_005" in (
        EVENTS_UPGRADE_SQL
    )
    assert "CREATE TABLE sessions__apdl_migration_005" in EVENTS_UPGRADE_SQL
    assert "toString(project_id)" in EVENTS_UPGRADE_SQL
    assert "EXCHANGE TABLES sessions AND sessions__apdl_migration_005" in (
        EVENTS_UPGRADE_SQL
    )


def test_clickhouse_runner_uses_an_exact_checksummed_ledger():
    migration_names = sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))
    assert [name.split("_", 1)[0] for name in migration_names] == [
        f"{version:03d}" for version in range(1, len(migration_names) + 1)
    ]
    assert "pipeline/clickhouse/migrate.py" in CLICKHOUSE_INIT_SCRIPT
    assert "apdl_schema_migrations" in CLICKHOUSE_MIGRATION_ENGINE
    assert "hashlib.sha256(payload).hexdigest()" in CLICKHOUSE_MIGRATION_ENGINE
    assert "checksum drift" in CLICKHOUSE_MIGRATION_ENGINE
    assert "ordered prefix" in CLICKHOUSE_MIGRATION_ENGINE
    assert "ReplacingMergeTree(applied_at)" in CLICKHOUSE_MIGRATION_ENGINE


def test_retryable_projection_tables_replace_by_project_and_message_id():
    assert "ENGINE = ReplacingMergeTree(first_exposure)" in FEATURE_FLAG_EXPOSURES_SQL
    assert "ORDER BY (project_id, message_id)" in FEATURE_FLAG_EXPOSURES_SQL

    assert "ENGINE = ReplacingMergeTree(timestamp)" in FRONTEND_HEALTH_EVENTS_SQL
    assert "ORDER BY (project_id, message_id)" in FRONTEND_HEALTH_EVENTS_SQL
    assert "ENGINE = MergeTree()" not in FRONTEND_HEALTH_EVENTS_SQL

    assert "ENGINE = ReplacingMergeTree(received_at)" in IDENTITY_ALIASES_SQL
    assert (
        "ORDER BY (project_id, message_id, anonymous_id, user_id)"
        in IDENTITY_ALIASES_SQL
    )
    assert "DROP TABLE IF EXISTS feature_flag_exposures" in (
        FEATURE_FLAG_EXPOSURES_SQL
    )
    assert "FROM events FINAL" in FEATURE_FLAG_EXPOSURES_SQL
    assert "DROP TABLE IF EXISTS frontend_health_events" in (
        FRONTEND_HEALTH_EVENTS_SQL
    )
    assert "FROM events FINAL" in FRONTEND_HEALTH_EVENTS_SQL


def test_duplicate_amplifying_aggregate_views_are_retired():
    assert "ENGINE = SummingMergeTree" not in MATERIALIZED_VIEWS_SQL
    assert "CREATE MATERIALIZED VIEW" not in MATERIALIZED_VIEWS_SQL
    assert "DROP TABLE IF EXISTS event_counts_hourly_mv" in MATERIALIZED_VIEWS_SQL
    assert "DROP TABLE IF EXISTS event_counts_daily_mv" in MATERIALIZED_VIEWS_SQL


def test_feature_flag_exposures_table_uses_canonical_variant_columns():
    assert "variant              LowCardinality(String)" in FEATURE_FLAG_EXPOSURES_SQL
    assert "rollout_bucket       Nullable(Float64)" in FEATURE_FLAG_EXPOSURES_SQL
    assert "variant_bucket       Nullable(Float64)" in FEATURE_FLAG_EXPOSURES_SQL
    assert "    value                Bool," not in FEATURE_FLAG_EXPOSURES_SQL
    assert "    bucket               Nullable(Float64)," not in FEATURE_FLAG_EXPOSURES_SQL


def test_feature_flag_exposures_mv_projects_canonical_event_properties():
    assert (
        "JSONExtractString(properties, 'variant') AS variant"
        in FEATURE_FLAG_EXPOSURES_SQL
    )
    assert (
        "JSONExtract(properties, 'rollout_bucket', 'Nullable(Float64)') "
        "AS rollout_bucket"
        in FEATURE_FLAG_EXPOSURES_SQL
    )
    assert (
        "JSONExtract(properties, 'variant_bucket', 'Nullable(Float64)') "
        "AS variant_bucket"
        in FEATURE_FLAG_EXPOSURES_SQL
    )
    assert "JSONExtractBool(properties, 'value')" not in FEATURE_FLAG_EXPOSURES_SQL
    assert "JSONExtract(properties, 'bucket', 'Nullable(Float64)')" not in (
        FEATURE_FLAG_EXPOSURES_SQL
    )


def test_legacy_experiment_exposure_storage_is_retired():
    assert "CREATE TABLE IF NOT EXISTS experiment_exposures" not in (
        LEGACY_EXPERIMENTS_SQL
    )
    assert "DROP TABLE IF EXISTS experiment_metrics_mv" in LEGACY_EXPERIMENTS_SQL
    assert "DROP TABLE IF EXISTS experiment_exposures" in LEGACY_EXPERIMENTS_SQL
    assert "CREATE MATERIALIZED VIEW IF NOT EXISTS experiment_metrics_mv" not in (
        MATERIALIZED_VIEWS_SQL
    )
    assert "INNER JOIN experiment_exposures" not in MATERIALIZED_VIEWS_SQL


def test_disconnected_prototype_schemas_are_removed_on_upgrade():
    for view in (
        "flag_evaluations_v",
        "experiment_exposures_v",
        "agent_actions_v",
        "personalizations_v",
    ):
        assert f"DROP VIEW IF EXISTS {view}" in PROTOTYPE_RETIREMENT_SQL

    for table in ("events_dlq_v2", "events_v2", "decisions_v2", "feeds_v2"):
        assert f"DROP TABLE IF EXISTS {table}" in PROTOTYPE_RETIREMENT_SQL

    assert "CREATE TABLE" not in PROTOTYPE_RETIREMENT_SQL


def test_identity_alias_projection_uses_identify_with_both_ids_as_only_assertion():
    assert "CREATE MATERIALIZED VIEW IF NOT EXISTS identity_alias_assertions_mv" in (
        IDENTITY_ALIASES_SQL
    )
    assert "FROM events\nWHERE event_type = 'identify'" in IDENTITY_ALIASES_SQL
    assert "AND user_id != ''" in IDENTITY_ALIASES_SQL
    assert "AND anonymous_id != ''" in IDENTITY_ALIASES_SQL
    assert "previous_id" not in IDENTITY_ALIASES_SQL
    assert "alias@" not in IDENTITY_ALIASES_SQL


def test_identity_alias_backfill_is_historical_checksummed_and_one_time():
    assert "FROM events FINAL" not in IDENTITY_ALIASES_SQL
    assert "FROM events FINAL" in IDENTITY_ALIASES_BACKFILL_SQL
    assert IDENTITY_ALIASES_SQL.index(
        "CREATE MATERIALIZED VIEW IF NOT EXISTS identity_alias_resolution_state_mv"
    ) < IDENTITY_ALIASES_SQL.index(
        "CREATE MATERIALIZED VIEW IF NOT EXISTS identity_alias_assertions_mv"
    )
    assert "apdl_schema_backfills FINAL" in CLICKHOUSE_INIT_SCRIPT
    assert "ClickHouse backfill checksum drift" in CLICKHOUSE_INIT_SCRIPT
    assert "ORDER BY (name, checksum)" in CLICKHOUSE_INIT_SCRIPT
    assert "mkdir \"$backfill_lock_dir\"" in CLICKHOUSE_INIT_SCRIPT
    assert "cp \"$backfill\" \"$backfill_snapshot\"" in CLICKHOUSE_INIT_SCRIPT
    assert "ClickHouse backfills directory not found" in CLICKHOUSE_INIT_SCRIPT
    assert CLICKHOUSE_INIT_SCRIPT.index("recorded_checksum=") < (
        CLICKHOUSE_INIT_SCRIPT.index("--multiquery < \"$backfill_snapshot\"")
    )


def test_resolved_identity_aliases_are_tenant_bound_and_conflicts_fail_closed():
    assert "CREATE VIEW IF NOT EXISTS resolved_identity_aliases" in (
        IDENTITY_ALIASES_SQL
    )
    assert "ENGINE = AggregatingMergeTree" in IDENTITY_ALIASES_SQL
    assert "minState(user_id) AS min_user_id" in IDENTITY_ALIASES_SQL
    assert "maxState(user_id) AS max_user_id" in IDENTITY_ALIASES_SQL
    assert "minMerge(min_user_id) = maxMerge(max_user_id)" in IDENTITY_ALIASES_SQL
    assert "AS has_conflict" in IDENTITY_ALIASES_SQL
    assert "uniqExact" not in IDENTITY_ALIASES_SQL
    assert "GROUP BY\n    project_id,\n    anonymous_id" in IDENTITY_ALIASES_SQL


def test_identity_alias_assertions_outlive_the_source_event_ttl():
    table_definition = IDENTITY_ALIASES_SQL.split(
        "CREATE TABLE IF NOT EXISTS identity_alias_assertions (", 1
    )[1].split("ALTER TABLE identity_alias_assertions", 1)[0]

    assert "TTL" not in table_definition
