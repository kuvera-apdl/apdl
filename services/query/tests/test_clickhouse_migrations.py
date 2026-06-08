"""Contract tests for ClickHouse analytics migrations."""

from pathlib import Path


MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[3] / "pipeline" / "clickhouse" / "migrations"
)
FEATURE_FLAG_EXPOSURES_SQL = (
    MIGRATIONS_DIR / "006_feature_flag_exposures.sql"
).read_text()
LEGACY_EXPERIMENTS_SQL = (MIGRATIONS_DIR / "003_experiments.sql").read_text()
MATERIALIZED_VIEWS_SQL = (MIGRATIONS_DIR / "004_materialized_views.sql").read_text()


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
