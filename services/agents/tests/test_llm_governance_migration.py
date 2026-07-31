"""Static contract checks for the canonical LLM governance migration."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SQL = (ROOT / "pipeline/postgres/migrations/023_llm_governance.sql").read_text()
XAI_SQL = (
    ROOT / "pipeline/postgres/migrations/045_xai_llm_provider.sql"
).read_text()
PROJECT_CREDENTIAL_ROUTING_SQL = (
    ROOT / "pipeline/postgres/migrations/049_project_scoped_llm_routing.sql"
).read_text()


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


def test_xai_provider_is_admitted_to_policy_and_attempt_ledgers():
    assert "ALTER TABLE llm_project_provider_policies" in XAI_SQL
    assert "DROP CONSTRAINT llm_project_provider_name_check" in XAI_SQL
    assert "ADD CONSTRAINT llm_project_provider_name_check" in XAI_SQL
    assert "ALTER TABLE llm_provider_attempts" in XAI_SQL
    assert "DROP CONSTRAINT llm_provider_attempts_provider_check" in XAI_SQL
    assert "ADD CONSTRAINT llm_provider_attempts_provider_check" in XAI_SQL
    assert XAI_SQL.count(
        "provider IN ('openai', 'anthropic', 'google', 'xai', 'local')"
    ) == 2


def test_project_routing_binds_tiers_and_attempts_to_exact_credentials():
    sql = PROJECT_CREDENTIAL_ROUTING_SQL

    assert "CREATE TABLE llm_project_model_assignments" in sql
    assert "PRIMARY KEY (project_id, tier)" in sql
    assert "tier IN ('fast', 'reasoning')" in sql
    assert "ADD COLUMN credential_id UUID" in sql
    assert "ADD COLUMN credential_version BIGINT" in sql
    assert "llm_provider_attempts_credential_fk" in sql
    assert "credential_id, project_id, provider, credential_version" in sql
    assert "llm_provider_attempts_credential_binding_check" in sql
    assert "llm_provider_attempts_protect_credential_binding" in sql
    assert "'credential_unavailable'" in sql
