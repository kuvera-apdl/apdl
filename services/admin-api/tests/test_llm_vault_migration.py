"""Static schema contract for Admin API LLM Vault mutation auditing."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = (
    ROOT
    / "pipeline"
    / "postgres"
    / "migrations"
    / "057_admin_proxy_audit_llm_vault.sql"
)
SQL = MIGRATION.read_text(encoding="utf-8")


def test_llm_vault_is_an_allowed_admin_proxy_audit_service() -> None:
    assert "DROP CONSTRAINT admin_proxy_audit_service_check" in SQL
    assert "ADD CONSTRAINT admin_proxy_audit_service_check" in SQL
    assert "'llm-vault'" in SQL
