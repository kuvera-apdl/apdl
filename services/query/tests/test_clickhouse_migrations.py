"""Contract tests for ClickHouse analytics migrations."""

from pathlib import Path


MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[3] / "pipeline" / "clickhouse" / "migrations"
)
FEATURE_FLAG_EXPOSURES_SQL = (
    MIGRATIONS_DIR / "006_feature_flag_exposures.sql"
).read_text()
EVENTS_SQL = (MIGRATIONS_DIR / "001_events.sql").read_text()
FRONTEND_HEALTH_EVENTS_SQL = (
    MIGRATIONS_DIR / "007_frontend_health_events.sql"
).read_text()
LEGACY_EXPERIMENTS_SQL = (MIGRATIONS_DIR / "003_experiments.sql").read_text()
MATERIALIZED_VIEWS_SQL = (MIGRATIONS_DIR / "004_materialized_views.sql").read_text()


def test_events_table_replaces_retries_by_project_and_message_id():
    assert "ENGINE = ReplacingMergeTree(received_at)" in EVENTS_SQL
    assert "ORDER BY (project_id, message_id)" in EVENTS_SQL
    assert "ENGINE = MergeTree()" not in EVENTS_SQL


def test_retryable_projection_tables_replace_by_project_and_message_id():
    assert "ENGINE = ReplacingMergeTree(first_exposure)" in FEATURE_FLAG_EXPOSURES_SQL
    assert "ORDER BY (project_id, message_id)" in FEATURE_FLAG_EXPOSURES_SQL

    assert "ENGINE = ReplacingMergeTree(timestamp)" in FRONTEND_HEALTH_EVENTS_SQL
    assert "ORDER BY (project_id, message_id)" in FRONTEND_HEALTH_EVENTS_SQL
    assert "ENGINE = MergeTree()" not in FRONTEND_HEALTH_EVENTS_SQL


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
