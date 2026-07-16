"""Release-boundary tests for the one supported event contract."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WRITER = (ROOT / "pipeline" / "redis" / "clickhouse_writer.py").read_text()
QUERY_SOURCES = "\n".join(
    path.read_text()
    for path in sorted((ROOT / "services" / "query" / "app").rglob("*.py"))
)
SUPPORTED_MIGRATIONS = ROOT / "pipeline" / "clickhouse" / "migrations"
PROTOTYPE_RETIREMENT = (
    SUPPORTED_MIGRATIONS / "012_retire_prototype_schemas.sql"
).read_text()
REMOVED_ETL_MANIFEST = ROOT / "pipeline" / "etl" / "pyproject.toml"
CLICKHOUSE_INIT = (ROOT / "scripts" / "init-clickhouse.sh").read_text()
CLICKHOUSE_MIGRATION_ENGINE = (
    ROOT / "pipeline" / "clickhouse" / "migrate.py"
).read_text()


def test_live_writer_and_query_use_the_events_table_only():
    assert '"INSERT INTO events ("' in WRITER
    assert "events_v2" not in WRITER
    assert "FROM events AS" in QUERY_SOURCES
    assert "events_v2" not in QUERY_SOURCES


def test_supported_migrations_only_retire_removed_prototype_v2_tables():
    migration_sql = "\n".join(
        path.read_text() for path in sorted(SUPPORTED_MIGRATIONS.glob("*.sql"))
    )

    for table in ("events_v2", "decisions_v2", "feeds_v2"):
        assert f"CREATE TABLE {table}" not in migration_sql
        assert f"CREATE TABLE IF NOT EXISTS {table}" not in migration_sql
        assert f"DROP TABLE IF EXISTS {table}" in PROTOTYPE_RETIREMENT
        assert table in CLICKHOUSE_MIGRATION_ENGINE

    assert not REMOVED_ETL_MANIFEST.exists()
    assert not list((ROOT / "pipeline").rglob("*_v2.sql"))
    assert "Unsupported prototype v2 schema operation in migration" in (
        CLICKHOUSE_MIGRATION_ENGINE
    )


def test_migrations_are_the_only_executable_clickhouse_schema_authority():
    schema_copies = ROOT / "pipeline" / "clickhouse" / "schemas"
    assert not list(schema_copies.glob("*.sql"))

    executable_event_ddl = [
        path
        for path in (ROOT / "pipeline" / "clickhouse").rglob("*.sql")
        if "CREATE TABLE IF NOT EXISTS events (" in path.read_text()
    ]
    assert executable_event_ddl == [SUPPORTED_MIGRATIONS / "001_events.sql"]


def test_runtime_services_do_not_publish_disconnected_envelope_models():
    removed_models = (
        ROOT / "services" / "ingestion" / "app" / "models" / "envelope.py",
        ROOT
        / "services"
        / "config"
        / "app"
        / "models"
        / "decision_envelope.py",
        ROOT / "services" / "agents" / "app" / "models" / "action_envelope.py",
    )

    assert all(not path.exists() for path in removed_models)

    ingestion_schema = (
        ROOT / "services" / "ingestion" / "app" / "models" / "schemas.py"
    ).read_text()
    ingestion_route = (
        ROOT / "services" / "ingestion" / "app" / "routers" / "events.py"
    ).read_text()
    assert "class Event(BaseModel):" in ingestion_schema
    assert "class EventBatch(BaseModel):" in ingestion_schema
    assert "validate_event_batch(body)" in ingestion_route
    assert 'stream_key = f"events:raw:{project_id}"' in ingestion_route
