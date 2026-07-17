"""Static contract checks for the canonical LLM governance migration."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SQL = (ROOT / "pipeline/postgres/migrations/023_llm_governance.sql").read_text()


def test_llm_governance_separates_logical_calls_and_provider_attempts():
    assert "CREATE TABLE llm_calls" in SQL
    assert "CREATE TABLE llm_provider_attempts" in SQL
    assert "UNIQUE (call_id, attempt_number)" in SQL
    assert "UNIQUE (project_id, run_id, call_id, execution_owner_id)" in SQL
    assert "FOREIGN KEY (project_id, run_id, call_id, execution_owner_id)" in SQL
    assert "provider TEXT NOT NULL" in SQL
    assert "model TEXT NOT NULL" in SQL
    assert "prompt_sha256 CHAR(64) NOT NULL" in SQL
    assert "egress_started_at TIMESTAMPTZ" in SQL
    assert "charged_cost_usd_micros BIGINT" in SQL
    assert "retryable BOOLEAN NOT NULL DEFAULT FALSE" in SQL


def test_llm_governance_default_is_local_only_and_cross_vendor_off():
    assert "required_data_residency TEXT NOT NULL DEFAULT 'local'" in SQL
    assert "allow_cross_vendor_retry BOOLEAN NOT NULL DEFAULT FALSE" in SQL
    assert (
        "'local',\n    'gemma4',\n    'http://localhost:11434/v1',\n    'local'"
        in SQL
    )
    assert "execution_owner_id TEXT NOT NULL" in SQL
    assert "allowed_data_classifications TEXT[] NOT NULL" in SQL
    assert "project_daily_cost_limit_usd_micros BIGINT NOT NULL DEFAULT 0" in SQL
    assert "run_cost_limit_usd_micros BIGINT NOT NULL DEFAULT 0" in SQL
    assert "CREATE TRIGGER admin_projects_ensure_llm_policy" in SQL


def test_llm_governance_budget_reservation_has_project_and_run_indexes():
    assert "project_daily_cost_limit_usd_micros" in SQL
    assert "run_cost_limit_usd_micros" in SQL
    assert "llm_provider_attempts_project_budget_idx" in SQL
    assert "llm_provider_attempts_run_budget_idx" in SQL
    assert "reserved_cost_usd_micros BIGINT NOT NULL" in SQL
