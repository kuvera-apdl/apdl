"""Contracts for the canonical PostgreSQL migration authority."""

import json
import re
from pathlib import Path

from app.framework.tool_catalog import TOOL_CATALOG


ROOT = Path(__file__).resolve().parents[3]
POSTGRES_MIGRATIONS = ROOT / "pipeline" / "postgres" / "migrations"
CLICKHOUSE_MIGRATIONS = ROOT / "pipeline" / "clickhouse" / "migrations"
AUTH_CREDENTIALS_SQL = (
    POSTGRES_MIGRATIONS / "001_auth_credentials.sql"
).read_text()
AGENTS_CORE_SQL = (POSTGRES_MIGRATIONS / "004_agents_core.sql").read_text()
OBSERVABILITY_SQL = (POSTGRES_MIGRATIONS / "005_agent_observability.sql").read_text()
CONFIG_SQL = (POSTGRES_MIGRATIONS / "006_config.sql").read_text()
CODEGEN_SQL = (POSTGRES_MIGRATIONS / "007_codegen.sql").read_text()
CODEGEN_PUBLICATION_IDENTITY_SQL = (
    POSTGRES_MIGRATIONS / "010_codegen_publication_identity.sql"
).read_text()
CODEGEN_DEVELOPMENT_PUBLICATION_SQL = (
    POSTGRES_MIGRATIONS / "011_codegen_development_publication.sql"
).read_text()
CODEGEN_SEGMENTED_PUBLICATION_SQL = (
    POSTGRES_MIGRATIONS / "024_codegen_segmented_publication.sql"
).read_text()
CUSTOM_AGENT_CONTRACT_SQL = (
    POSTGRES_MIGRATIONS / "015_custom_agent_contracts_and_retry_lineage.sql"
).read_text()
RETENTION_COHORT_MODE_SQL = (
    POSTGRES_MIGRATIONS / "019_retention_cohort_mode.sql"
).read_text()
AGENTS_GOVERNANCE_SQL = (
    POSTGRES_MIGRATIONS / "020_agents_governance.sql"
).read_text()
AGENTS_MUTATION_QUOTAS_SQL = (
    POSTGRES_MIGRATIONS / "021_agents_mutation_quotas.sql"
).read_text()
CONFIG_LEGACY_FIXTURE = (
    ROOT
    / "pipeline"
    / "postgres"
    / "tests"
    / "fixtures"
    / "legacy_config_restrictive.sql"
).read_text()
POSTGRES_RUNNER = (ROOT / "scripts" / "init-postgres.sh").read_text()
MIGRATION_ENGINE = (ROOT / "pipeline" / "postgres" / "migrate.py").read_text()
CLICKHOUSE_RUNNER = (ROOT / "scripts" / "init-clickhouse.sh").read_text()


def _table_definition(sql: str, table: str) -> str:
    start = sql.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    return sql[start : sql.index("\n);", start) + 3]


def test_postgres_migrations_are_strictly_ordered_and_uniquely_versioned():
    names = sorted(path.name for path in POSTGRES_MIGRATIONS.glob("*.sql"))
    assert all(re.fullmatch(r"[0-9]{3}_[a-z0-9_]+\.sql", name) for name in names)
    versions = [name.split("_", 1)[0] for name in names]
    assert versions == [f"{version:03d}" for version in range(1, len(names) + 1)]
    assert len(versions) == len(set(versions))


def test_clickhouse_directory_contains_no_postgres_migrations():
    names = {path.name for path in CLICKHOUSE_MIGRATIONS.glob("*.sql")}
    assert "005_pgvector_setup.sql" not in names
    assert "011_envelope_postgres.sql" not in names

    for migration in CLICKHOUSE_MIGRATIONS.glob("*.sql"):
        sql = migration.read_text().lower()
        assert "target: postgresql" not in sql
        assert "not clickhouse" not in sql
        assert "create extension if not exists vector" not in sql


def test_auth_credentials_enforce_confidential_and_browser_kinds():
    credentials = _table_definition(AUTH_CREDENTIALS_SQL, "auth_credentials")

    assert "credential_kind TEXT NOT NULL" in credentials
    assert "credential_kind IN ('confidential', 'browser')" in credentials
    assert "key_prefix = 'proj_' || project_id || '_'" in credentials
    assert "key_prefix = 'client_' || project_id || '_'" in credentials
    assert "credential_kind = 'browser'" in credentials
    assert "('events:write' = ANY(roles))::INT" in credentials
    assert "('agents:approve' = ANY(roles))::INT" in credentials
    assert "cardinality(roles) = 2" in credentials
    assert "roles @> ARRAY[" in credentials
    assert "roles <@ ARRAY[" in credentials

    assert "APDL_DEV_CLIENT_KEY" in POSTGRES_RUNNER
    assert '"local-dev-confidential"' in POSTGRES_RUNNER
    assert '"local-dev-browser"' in POSTGRES_RUNNER
    assert "credential_kind, key_prefix, key_hash, roles" in POSTGRES_RUNNER


def test_agents_core_migration_matches_the_running_service_contracts():
    memory = _table_definition(AGENTS_CORE_SQL, "agent_memory")
    runs = _table_definition(AGENTS_CORE_SQL, "agent_runs")
    audit = _table_definition(AGENTS_CORE_SQL, "agent_audit_log")

    assert "id BIGSERIAL PRIMARY KEY" in memory
    assert "embedding vector(384)" in memory
    assert "agent_type TEXT" not in memory
    assert "run_id TEXT PRIMARY KEY" in runs
    assert "config JSONB DEFAULT '{}'" in runs
    assert "lease_owner_id TEXT" in runs
    assert "lease_expires_at TIMESTAMPTZ" in runs
    assert "idx_agent_runs_lease_expiry" in AGENTS_CORE_SQL
    assert "claim_run_id TEXT" in AGENTS_CORE_SQL
    assert "idx_feature_proposals_claim_run" in AGENTS_CORE_SQL
    assert "id BIGSERIAL PRIMARY KEY" in audit
    assert "action_type TEXT NOT NULL" in audit
    assert "config JSONB DEFAULT '{}'" in audit

    assert "CREATE TABLE IF NOT EXISTS experiments (" not in AGENTS_CORE_SQL
    assert "CREATE TABLE IF NOT EXISTS ui_configs (" not in AGENTS_CORE_SQL
    assert "agent_memory_legacy_005" in AGENTS_CORE_SQL
    assert "agent_runs_legacy_005" in AGENTS_CORE_SQL
    assert "agent_audit_log_legacy_005" in AGENTS_CORE_SQL
    assert "experiments_legacy_005" in AGENTS_CORE_SQL
    assert "ui_configs_legacy_005" in AGENTS_CORE_SQL
    assert "agent_memory_legacy_vectors" in AGENTS_CORE_SQL
    assert AGENTS_CORE_SQL.index("CREATE TABLE agent_memory_legacy_vectors") < (
        AGENTS_CORE_SQL.index("DELETE FROM agent_memory")
    )


def test_agents_governance_scopes_feature_proposal_identity_to_project():
    assert "DROP CONSTRAINT feature_proposals_pkey" in AGENTS_GOVERNANCE_SQL
    assert "PRIMARY KEY (project_id, proposal_id)" in AGENTS_GOVERNANCE_SQL


def test_agents_mutation_quota_ledger_has_one_strict_canonical_identity():
    sql = AGENTS_MUTATION_QUOTAS_SQL

    assert "CREATE TABLE agent_mutation_quota_reservations" in sql
    assert "PRIMARY KEY (project_id, action_type, idempotency_key)" in sql
    assert "policy_version = 'rolling_hour@1'" in sql
    assert "agent_mutation_quota_action_type_check" in sql
    assert "agent_mutation_quota_idempotency_key_check" in sql
    assert "btrim(idempotency_key) <> ''" in sql
    assert "agent_mutation_quota_reservations_lookup_idx" in sql
    assert "(project_id, action_type, policy_version, occurred_at DESC)" in sql


def test_observability_migration_uses_text_tenant_and_run_identifiers():
    llm_calls = _table_definition(OBSERVABILITY_SQL, "llm_calls")

    assert "project_id TEXT NOT NULL" in llm_calls
    assert "project_id INTEGER" not in llm_calls
    assert (
        "run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE"
        in llm_calls
    )
    assert "REFERENCES agent_runs(id)" not in llm_calls
    assert "ON agent_audit_log (run_id, idempotency_key)" in OBSERVABILITY_SQL
    assert "llm_calls_legacy_011" in OBSERVABILITY_SQL


def test_config_and_codegen_have_canonical_migrations():
    assert "CHECK (state IN ('draft', 'active'))" in CONFIG_LEGACY_FIXTURE
    assert "CHECK (status IN ('draft', 'active', 'completed', 'stopped'))" in (
        CONFIG_LEGACY_FIXTURE
    )
    assert "CREATE TABLE IF NOT EXISTS flags (" in CONFIG_SQL
    assert "CREATE TABLE IF NOT EXISTS experiments (" in CONFIG_SQL
    assert "feature_flags_legacy" in CONFIG_SQL
    assert CONFIG_SQL.index("DROP CONSTRAINT IF EXISTS experiments_status_check") < (
        CONFIG_SQL.index("SET status = 'running' WHERE status = 'active'")
    )
    assert "CREATE TABLE IF NOT EXISTS codegen_changesets (" in CODEGEN_SQL
    assert "CREATE TABLE IF NOT EXISTS codegen_ci_verification_observations (" in (
        CODEGEN_SQL
    )
    assert "codegen_runtime_evidence_observations_legacy_unbound" in CODEGEN_SQL


def test_codegen_publication_identity_migration_archives_without_fabrication():
    sql = CODEGEN_PUBLICATION_IDENTITY_SQL
    assert "publication_authorization_legacy" in sql
    assert "publication_authorization = NULL" in sql
    assert "IS DISTINCT FROM 'publication_authorization@2'" in sql
    assert "= 'publication_authorization@2'" in sql
    assert ") IS TRUE" in sql
    assert "publication_authorization@1" in sql
    assert "jsonb_set" not in sql.lower()


def test_codegen_development_publication_is_a_separate_draft_only_contract():
    sql = CODEGEN_DEVELOPMENT_PUBLICATION_SQL
    assert "publication_authorization@2" in sql
    assert "development_publication_authorization@1" in sql
    assert "development_publication_request@1" in sql
    assert "development_publication_decision@1" in sql
    assert "local_development" in sql
    assert "development_pr" in sql
    assert "local-development" in sql
    assert "draft_only" in sql
    assert ") IS TRUE" in sql


def test_codegen_segmented_publication_retires_aggregate_only_authority():
    sql = CODEGEN_SEGMENTED_PUBLICATION_SQL
    assert "publication_authorization_segmentless_legacy" in sql
    assert "publication_authorization = NULL" in sql
    assert "= 'publication_authorization@2'" in sql
    assert "= 'publication_authorization@3'" in sql
    assert "development_publication_authorization@1" in sql
    assert "segmented evidence" in sql
    assert ") IS TRUE" in sql
    assert "jsonb_set" not in sql.lower()


def test_custom_agent_contract_migration_is_explicit_and_strict():
    sql = CUSTOM_AGENT_CONTRACT_SQL
    legacy_tools = [
        "discover_events",
        "query_events",
        "query_timeseries",
        "query_funnel",
        "query_retention",
        "query_cohort",
        "query_breakdown",
        "list_flags",
        "get_active_experiments",
    ]
    for tool_name in legacy_tools:
        assert f'"{tool_name}"' in sql
    migrated_tools = re.search(r"SET tools = '(\[[^']+\])'::jsonb", sql)
    assert migrated_tools is not None
    assert json.loads(migrated_tools.group(1)) == legacy_tools
    assert "calculate_statistical_plan" in TOOL_CATALOG
    assert '"calculate_statistical_plan"' not in sql
    assert "WHERE tools = '[]'::jsonb" in sql
    assert "DROP COLUMN IF EXISTS parse_as" in sql
    assert "CREATE TABLE custom_agent_test_runs" in sql
    assert "llm_calls INTEGER NOT NULL DEFAULT 0" in sql
    assert "custom_agent_test_runs_one_running_per_project_idx" in sql
    assert "WHERE status = 'running'" in sql


def test_retry_lineage_migration_backfills_one_child_and_enforces_uniqueness():
    sql = CUSTOM_AGENT_CONTRACT_SQL
    assert "ADD COLUMN IF NOT EXISTS retry_of_changeset_id TEXT" in sql
    assert "row_number() OVER" in sql
    assert "legacy.retry_rank = 1" in sql
    assert "codegen_changesets_retry_of_changeset_id_fkey" in sql
    assert "codegen_changesets_one_retry_child_idx" in sql
    assert "WHERE retry_of_changeset_id IS NOT NULL" in sql


def test_retention_preset_migration_is_narrow_and_order_preserving():
    sql = RETENTION_COHORT_MODE_SQL

    assert "WITH ORDINALITY" in sql
    assert "ORDER BY entry.ordinality" in sql
    assert "WHEN jsonb_typeof(agent.preset_tools) = 'array'" in sql
    assert "ELSE '[]'::jsonb" in sql
    assert "entry.value ->> 'tool' = 'query_retention'" in sql
    assert "jsonb_typeof(entry.value -> 'params') = 'object'" in sql
    assert "NOT (entry.value -> 'params' ? 'cohort_mode')" in sql
    assert "'{params,cohort_mode}'" in sql
    assert "to_jsonb('first_match_in_window'::text)" in sql
    assert "agent.preset_tools IS DISTINCT FROM migrated.preset_tools" in sql


def test_database_runners_enforce_the_single_engine_authority():
    assert "postgres-migrate" in POSTGRES_RUNNER
    assert "apdl_schema_migrations" in MIGRATION_ENGINE
    assert "hashlib.sha256" in MIGRATION_ENGINE
    assert "pg_advisory_xact_lock" in MIGRATION_ENGINE
    assert "Migration checksum or name drift detected" in MIGRATION_ENGINE
    assert "apdl_reject_migration_ledger_mutation" in MIGRATION_ENGINE
    assert "Misplaced PostgreSQL migration" in CLICKHOUSE_RUNNER
    assert "Skipping $(basename" not in CLICKHOUSE_RUNNER
