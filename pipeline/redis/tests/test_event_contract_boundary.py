"""Release-boundary tests for the one supported event contract."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WRITER = (ROOT / "pipeline" / "redis" / "clickhouse_writer.py").read_text()
QUERY_SOURCES = "\n".join(
    path.read_text()
    for path in sorted((ROOT / "services" / "query" / "app").rglob("*.py"))
)
SUPPORTED_MIGRATIONS = ROOT / "pipeline" / "clickhouse" / "migrations"
REMOVED_ETL_MANIFEST = ROOT / "pipeline" / "etl" / "pyproject.toml"
CLICKHOUSE_INIT = (ROOT / "scripts" / "init-clickhouse.sh").read_text()


def test_live_writer_and_query_use_the_events_table_only():
    assert '"INSERT INTO events ("' in WRITER
    assert "events_v2" not in WRITER
    assert "FROM events AS" in QUERY_SOURCES
    assert "events_v2" not in QUERY_SOURCES


def test_supported_migrations_do_not_create_removed_prototype_v2_tables():
    migration_sql = "\n".join(
        path.read_text() for path in sorted(SUPPORTED_MIGRATIONS.glob("*.sql"))
    )

    for table in ("events_v2", "decisions_v2", "feeds_v2"):
        assert table not in migration_sql
        assert table in CLICKHOUSE_INIT

    assert not REMOVED_ETL_MANIFEST.exists()
    assert not list((ROOT / "pipeline").rglob("*_v2.sql"))
    assert "Unsupported prototype v2 schema in release migration" in CLICKHOUSE_INIT


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
