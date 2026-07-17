"""Tests for fail-fast Codegen schema validation."""

from pathlib import Path

import pytest

from app.db import MIGRATION_NAME, MIGRATION_VERSION, REQUIRED_COLUMNS, assert_schema_ready


class FakeConn:
    def __init__(
        self,
        *,
        ledger_exists: bool = True,
        migration_name: str | None = MIGRATION_NAME,
        columns=REQUIRED_COLUMNS,
    ):
        self.ledger_exists = ledger_exists
        self.migration_name = migration_name
        self.columns = set(columns)

    async def fetchval(self, sql: str, *args):
        if "to_regclass" in sql:
            return self.ledger_exists
        if "apdl_schema_migrations" in sql:
            return self.migration_name
        raise AssertionError(sql)

    async def fetch(self, sql: str, *args):
        assert "information_schema.columns" in sql
        return [
            {"table_name": table, "column_name": column}
            for table, column in self.columns
        ]


@pytest.mark.asyncio
async def test_accepts_complete_migrated_schema():
    await assert_schema_ready(FakeConn())


def test_startup_requires_execution_authority_migration():
    assert MIGRATION_VERSION == 28
    assert MIGRATION_NAME == "028_admin_execution_authority.sql"
    assert (
        "admin_project_execution_authorizations",
        "authorization_source",
    ) in REQUIRED_COLUMNS


@pytest.mark.asyncio
async def test_rejects_missing_migration_ledger():
    with pytest.raises(RuntimeError, match="migration ledger is missing"):
        await assert_schema_ready(FakeConn(ledger_exists=False))


@pytest.mark.asyncio
async def test_rejects_database_without_execution_authority_migration():
    with pytest.raises(RuntimeError, match="028_admin_execution_authority.sql"):
        await assert_schema_ready(
            FakeConn(migration_name="027_codegen_pr_publication_recovery.sql")
        )


@pytest.mark.asyncio
async def test_rejects_incomplete_schema_at_startup():
    columns = REQUIRED_COLUMNS - {("github_repository_grants", "repository_id")}
    with pytest.raises(RuntimeError, match="github_repository_grants.repository_id"):
        await assert_schema_ready(FakeConn(columns=columns))


@pytest.mark.asyncio
async def test_rejects_missing_execution_authorization_contract_at_startup():
    columns = REQUIRED_COLUMNS - {
        ("admin_project_execution_authorizations", "actor")
    }
    with pytest.raises(
        RuntimeError,
        match="admin_project_execution_authorizations.actor",
    ):
        await assert_schema_ready(FakeConn(columns=columns))


def test_codegen_startup_contains_no_postgres_ddl():
    app_dir = Path(__file__).parents[1] / "app"
    main_source = (app_dir / "main.py").read_text()
    db_source = (app_dir / "db.py").read_text()
    assert "CREATE TABLE" not in main_source
    assert "ALTER TABLE" not in main_source
    assert "CREATE TABLE" not in db_source
    assert "ALTER TABLE" not in db_source


def test_durable_effects_migration_defines_strict_changeset_idempotency():
    migration = (
        Path(__file__).parents[3]
        / "pipeline/postgres/migrations/022_agents_durable_effects.sql"
    ).read_text()

    assert "ADD COLUMN IF NOT EXISTS idempotency_key TEXT" in migration
    assert "ADD COLUMN IF NOT EXISTS idempotency_request_sha256 CHAR(64)" in migration
    assert "ALTER COLUMN idempotency_key SET NOT NULL" in migration
    assert "ALTER COLUMN idempotency_request_sha256 SET NOT NULL" in migration
    assert "codegen_changesets_idempotency_key_check" in migration
    assert "codegen_changesets_idempotency_request_sha256_check" in migration
    assert "'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$'" in migration
    assert "ON codegen_changesets (project_id, idempotency_key)" in migration
    assert "FOREIGN KEY (project_id, retry_of_changeset_id)" in migration
    assert "REFERENCES codegen_changesets(project_id, changeset_id)" in migration
    assert "'legacy:'\n        || md5(" in migration


def test_private_task_controls_migration_enforces_authority_boundary():
    migration = (
        Path(__file__).parents[3]
        / "pipeline/postgres/migrations/025_codegen_private_task_controls.sql"
    ).read_text()

    assert "ADD COLUMN IF NOT EXISTS control_metadata JSONB NOT NULL" in migration
    assert "changeset_controls@1" in migration
    assert "codegen_changesets_public_task_context_check" in migration
    assert "codegen_changesets_control_metadata_check" in migration
    assert "codegen changeset control metadata is immutable" in migration
    assert "private revert control requires a merged source" in migration
    for key in (
        "risk_level",
        "revert_sha",
        "reverts_changeset",
        "reverts_pr_number",
        "retry_of",
    ):
        assert key in migration


def test_egress_publication_migration_retires_unattested_authority():
    migration = (
        Path(__file__).parents[3]
        / "pipeline/postgres/migrations/026_codegen_egress_publication.sql"
    ).read_text()

    assert (
        "ADD COLUMN IF NOT EXISTS\n"
        "        publication_authorization_egress_unattested_legacy JSONB" in migration
    )
    assert "= 'publication_authorization@3'" in migration
    assert "= 'publication_authorization@4'" in migration
    assert "= 'publication_request@3'" in migration
    assert "expected_egress_policy_sha256" in migration
    assert (
        "publication_authorization->'request'->>'egress_policy_sha256'\n"
        "                = publication_authorization->>'expected_egress_policy_sha256'"
        in migration
    )
    assert "= 'development_publication_authorization@1'" in migration


def test_pr_publication_recovery_migration_is_strict_and_append_only():
    migration = (
        Path(__file__).parents[3]
        / "pipeline/postgres/migrations/027_codegen_pr_publication_recovery.sql"
    ).read_text()

    assert (
        "CREATE TABLE IF NOT EXISTS codegen_pull_request_publication_events"
        in migration
    )
    assert "pull_request_publication_intent@1" in migration
    assert "pull_request_create_accepted@1" in migration
    assert "pull_request_identity_validated@1" in migration
    assert "event_sequence BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE" in migration
    assert "cleanup_request_event_id TEXT" in migration
    assert "uq_codegen_pr_publication_intent" in migration
    assert "codegen_pr_publication_payload_identity_check" in migration
    assert "IS NOT DISTINCT FROM event_id" in migration
    assert "IS NOT DISTINCT FROM cleanup_request_event_id" in migration
    assert "request.pr_number IS NOT DISTINCT FROM NEW.pr_number" in migration
    assert "request.github_url IS NOT DISTINCT FROM NEW.github_url" in migration
    assert "NEW.payload->>'next_action'" in migration
    assert "codegen_pr_publication_events_require_intent" in migration
    assert "BEFORE UPDATE OR DELETE" in migration


def test_shutdown_awaits_requeued_jobs_before_closing_database():
    main_source = (Path(__file__).parents[1] / "app" / "main.py").read_text()
    await_requeued = "await asyncio.gather(*requeued_jobs, return_exceptions=True)"
    assert main_source.index(await_requeued) < main_source.index("await pool.close()")
