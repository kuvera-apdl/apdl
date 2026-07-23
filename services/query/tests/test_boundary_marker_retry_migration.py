from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "pipeline" / "postgres" / "migrations"
SQL = (MIGRATIONS / "041_boundary_marker_retry_quarantine.sql").read_text()


def test_boundary_marker_retry_migration_is_bounded_and_durable():
    assert "marker_publish_state TEXT NOT NULL DEFAULT 'pending'" in SQL
    assert "marker_publish_attempts SMALLINT NOT NULL DEFAULT 0" in SQL
    assert "marker_publish_next_attempt_at TIMESTAMPTZ DEFAULT now()" in SQL
    assert "marker_publish_attempts BETWEEN 0 AND 5" in SQL
    assert "marker_publish_attempts = 5" in SQL
    assert "marker_publish_state = 'quarantined'" in SQL
    assert "marker_publish_quarantined_at TIMESTAMPTZ" in SQL
    assert "marker_publish_observed_stream_id TEXT" in SQL
    assert "marker_publish_observed_stream_id = marker_stream_id" in SQL


def test_boundary_marker_failures_use_one_fixed_safe_vocabulary():
    for failure_code in (
        "event_stream_capacity",
        "redis_publish_failed",
        "invalid_redis_marker_id",
        "boundary_authority_update_failed",
        "boundary_authority_update_invalid",
        "invalid_boundary_marker_dedup",
        "invalid_stream_authority",
        "invalid_marker_token",
        "unexpected_publish_failure",
    ):
        assert failure_code in SQL


def test_boundary_marker_state_machine_preserves_identity_and_terminal_states():
    assert "experiment analysis boundary identity is immutable" in SQL
    assert "experiment analysis boundary publication is terminal" in SQL
    assert "boundary marker retry attempt must advance once" in SQL
    assert "boundary marker success cannot change attempts" in SQL
    assert "boundary marker quarantine must advance once" in SQL
    assert "idx_experiment_analysis_boundaries_publish_due" in SQL
    assert "experiment_analysis_boundaries_observed_stream_identity" in SQL


def test_boundary_marker_retry_uses_assigned_migration_version():
    assert (
        MIGRATIONS / "041_boundary_marker_retry_quarantine.sql"
    ).is_file()
