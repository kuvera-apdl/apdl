"""Source contract for the shared-vault fresh-database preflight."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = (
    ROOT
    / "pipeline"
    / "postgres"
    / "migrations"
    / "056_project_llm_credential_vault.sql"
).read_text(encoding="utf-8")


def test_legacy_credential_preflight_rejects_any_retained_history() -> None:
    start = MIGRATION.index(
        "DO $apdl_require_empty_legacy_llm_credential_stores$"
    )
    end = MIGRATION.index("DO $apdl_ensure_llm_vault_role$")
    preflight = MIGRATION[start:end]

    assert (
        "EXISTS (SELECT 1 FROM llm_project_provider_credentials)" in preflight
    )
    assert (
        "EXISTS (SELECT 1 FROM codegen_project_provider_credentials)" in preflight
    )
    assert "WHERE state" not in preflight
    assert "revocation does not remove legacy credential history" in preflight
