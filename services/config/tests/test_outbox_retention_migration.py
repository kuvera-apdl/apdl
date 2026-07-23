"""Contracts for bounded Config outbox retention and exposure receipts."""

from pathlib import Path

from app import outbox, schema


MIGRATION_SQL = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "postgres"
    / "migrations"
    / "036_config_outbox_retention.sql"
).read_text()


def test_migration_backfills_canonical_receipts_before_enabling_cleanup():
    create = MIGRATION_SQL.index("CREATE TABLE config_exposure_receipts")
    backfill = MIGRATION_SQL.index("INSERT INTO config_exposure_receipts")
    cleanup_index = MIGRATION_SQL.index("idx_config_outbox_cleanup_processed")

    assert create < backfill < cleanup_index
    assert "PRIMARY KEY (project_id, message_id)" in MIGRATION_SQL
    assert "payload #- '{event,timestamp}'" in MIGRATION_SQL
    assert "NOT ((canonical_payload -> 'event') ? 'timestamp')" in MIGRATION_SQL
    assert "canonical_payload #>> '{event,message_id}' = message_id" in MIGRATION_SQL
    assert "FROM config_outbox" in MIGRATION_SQL
    assert "WHERE kind = 'exposure'" in MIGRATION_SQL


def test_migration_indexes_every_ordered_cleanup_horizon():
    assert "idx_config_exposure_receipts_cleanup" in MIGRATION_SQL
    assert "(last_seen_at, project_id, message_id)" in MIGRATION_SQL
    assert "idx_config_outbox_cleanup_processed" in MIGRATION_SQL
    assert "(processed_at, id)" in MIGRATION_SQL
    assert "idx_config_outbox_cleanup_quarantined" in MIGRATION_SQL
    assert "(quarantined_at, id)" in MIGRATION_SQL


def test_schema_gate_requires_receipt_ledger_migration():
    assert schema.MIGRATION_VERSION >= 36
    assert ("config_exposure_receipts", "project_id") in schema.REQUIRED_COLUMNS
    assert ("config_exposure_receipts", "message_id") in schema.REQUIRED_COLUMNS
    assert (
        "config_exposure_receipts",
        "canonical_payload",
    ) in schema.REQUIRED_COLUMNS
    assert ("config_exposure_receipts", "last_seen_at") in schema.REQUIRED_COLUMNS


def test_receipt_horizon_exceeds_event_ttl_with_margin():
    assert (
        outbox.EXPOSURE_RECEIPT_RETENTION_SECONDS
        > outbox.CLICKHOUSE_EVENT_RETENTION_MAX_SECONDS
    )
    assert (
        outbox.EXPOSURE_RECEIPT_RETENTION_SECONDS
        - outbox.CLICKHOUSE_EVENT_RETENTION_MAX_SECONDS
        >= 30 * 24 * 60 * 60
    )
