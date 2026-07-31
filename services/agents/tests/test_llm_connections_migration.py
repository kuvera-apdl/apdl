"""Static contracts for canonical project LLM connections."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SQL = (
    ROOT
    / "pipeline"
    / "postgres"
    / "migrations"
    / "050_llm_provider_connections.sql"
).read_text()


def test_connection_migration_is_strict_non_secret_and_audited() -> None:
    assert "CREATE TABLE llm_project_provider_connections" in SQL
    assert "CREATE TABLE llm_project_provider_models" in SQL
    assert "CREATE TABLE llm_project_provider_connection_audit" in SQL
    assert "PRIMARY KEY (project_id, provider)" in SQL
    assert "UNIQUE (project_id, provider, version)" in SQL
    assert "provider IN ('openai', 'anthropic', 'google', 'xai')" in SQL
    assert "schema_version = 'llm_provider_model@1'" in SQL
    assert "pricing_status = 'operator_review_required'" in SQL
    assert "llm_project_provider_connection_audit_no_update_delete" in SQL
    assert "llm_project_provider_connection_audit_no_truncate" in SQL

    lowered = SQL.lower()
    assert "api_key" not in lowered
    assert "ciphertext" not in lowered
    assert "nonce" not in lowered


def test_connection_inventory_uses_canonical_arrays() -> None:
    assert "ARRAY['fast', 'reasoning']::TEXT[]" in SQL
    assert "'public', 'internal', 'confidential', 'restricted'" in SQL
