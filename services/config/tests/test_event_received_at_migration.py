"""Contracts for strict receipt-time authority on Config exposure delivery."""

from pathlib import Path

from app import schema


MIGRATION_SQL = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "postgres"
    / "migrations"
    / "039_event_received_at_contract.sql"
).read_text()


def test_pending_exposures_gain_the_original_server_generated_time():
    assert "UPDATE config_outbox" in MIGRATION_SQL
    assert "'{event,server_timestamp}'" in MIGRATION_SQL
    assert "payload #> '{event,timestamp}'" in MIGRATION_SQL
    assert "kind = 'exposure'" in MIGRATION_SQL
    assert "processed_at IS NULL" in MIGRATION_SQL
    assert "quarantined_at IS NULL" in MIGRATION_SQL


def test_receipts_exclude_both_generated_event_times():
    assert "config_exposure_receipts_server_timestamp_check" in MIGRATION_SQL
    assert "? 'server_timestamp'" in MIGRATION_SQL
    assert schema.MIGRATION_VERSION >= 39
