"""Static contracts for PostgreSQL runtime and audited-operator separation."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FULL_COMPOSE = (ROOT / "infra/docker/docker-compose.yml").read_text()
DEPS_COMPOSE = (ROOT / "infra/docker/docker-compose.deps.yml").read_text()
BOOTSTRAP = (
    ROOT / "infra/docker/postgres/init-apdl-roles.sh"
).read_text()
PRIVILEGE_PROBE = (
    ROOT / "scripts/test_postgres_operator_privileges.sh"
).read_text()
MIGRATION = (
    ROOT
    / "pipeline/postgres/migrations/044_operator_recovery_and_retention.sql"
).read_text()
CAPABILITY_MIGRATION = (
    ROOT
    / "pipeline/postgres/migrations/058_agent_service_capabilities.sql"
).read_text()
FRESH_SMOKE = (ROOT / "scripts/smoke_fresh_install.sh").read_text()
CI = (ROOT / ".github/workflows/ci.yml").read_text()
SERVICE_DATABASE_SOURCES = [
    ROOT / "services/ingestion/app/main.py",
    ROOT / "services/config/app/main.py",
    ROOT / "services/query/app/main.py",
    ROOT / "services/codegen/app/config.py",
    ROOT / "services/admin-api/app/config.py",
]
AGENTS_DATABASE_SOURCE = ROOT / "services/agents/app/main.py"
VAULT_DATABASE_SOURCE = ROOT / "services/llm-vault/app/config.py"


class PostgresRoleContractTests(unittest.TestCase):
    def test_fresh_compose_bootstraps_roles_before_migrations(self) -> None:
        mount = (
            "./postgres/init-apdl-roles.sh:"
            "/docker-entrypoint-initdb.d/10-apdl-roles.sh:ro"
        )
        for compose in (FULL_COMPOSE, DEPS_COMPOSE):
            self.assertIn(mount, compose)
            self.assertIn(
                "APDL_RUNTIME_POSTGRES_PASSWORD: "
                "${APDL_RUNTIME_POSTGRES_PASSWORD:-apdl_runtime_dev}",
                compose,
            )
            self.assertIn(
                "APDL_AGENTS_POSTGRES_PASSWORD: "
                "${APDL_AGENTS_POSTGRES_PASSWORD:-apdl_agents_dev1}",
                compose,
            )
            self.assertIn(
                "APDL_LLM_VAULT_POSTGRES_PASSWORD: "
                "${APDL_LLM_VAULT_POSTGRES_PASSWORD:-apdl_llm_vault_dev}",
                compose,
            )
            self.assertIn("PGUSER: apdl", compose)
            self.assertIn("PGPASSWORD: apdl_dev", compose)

    def test_every_compose_service_uses_the_runtime_login(self) -> None:
        runtime_url = (
            "POSTGRES_URL: postgresql://apdl_runtime:"
            "${APDL_RUNTIME_POSTGRES_PASSWORD:-apdl_runtime_dev}"
            "@postgres:5432/apdl"
        )
        self.assertEqual(FULL_COMPOSE.count(runtime_url), 6)
        self.assertNotIn(
            "POSTGRES_URL: postgresql://apdl:apdl_dev@postgres:5432/apdl",
            FULL_COMPOSE,
        )
        vault_url = (
            "POSTGRES_URL: postgresql://apdl_llm_vault:"
            "${APDL_LLM_VAULT_POSTGRES_PASSWORD:-apdl_llm_vault_dev}"
            "@postgres:5432/apdl"
        )
        self.assertEqual(FULL_COMPOSE.count(vault_url), 1)
        agents_url = (
            "POSTGRES_URL: postgresql://apdl_agents:"
            "${APDL_AGENTS_POSTGRES_PASSWORD:-apdl_agents_dev1}"
            "@postgres:5432/apdl"
        )
        self.assertEqual(FULL_COMPOSE.count(agents_url), 1)

    def test_service_fallbacks_cannot_select_the_migration_owner(self) -> None:
        runtime_url = (
            "postgresql://apdl_runtime:"
            "apdl_runtime_dev@localhost:5432/apdl"
        )
        for path in SERVICE_DATABASE_SOURCES:
            source = path.read_text()
            self.assertIn(runtime_url, source, path)
            self.assertNotIn(
                "postgresql://apdl:apdl_dev@localhost:5432/apdl",
                source,
                path,
            )
        vault_source = VAULT_DATABASE_SOURCE.read_text()
        self.assertIn(
            "postgresql://apdl_llm_vault:apdl_llm_vault_dev@localhost:5432/apdl",
            vault_source,
        )
        self.assertNotIn(
            "postgresql://apdl_runtime:apdl_runtime_dev@localhost:5432/apdl",
            vault_source,
        )
        agents_source = AGENTS_DATABASE_SOURCE.read_text()
        self.assertIn(
            "postgresql://apdl_agents:apdl_agents_dev1@localhost:5432/apdl",
            agents_source,
        )
        self.assertNotIn(
            "postgresql://apdl_runtime:apdl_runtime_dev@localhost:5432/apdl",
            agents_source,
        )

    def test_bootstrap_roles_are_fixed_and_unprivileged(self) -> None:
        for role in (
            "apdl_runtime",
            "apdl_agents",
            "apdl_llm_vault",
            "apdl_audit_operator",
            "apdl_audit_purge_definer",
            "apdl_project_authority_definer",
            "apdl_capability_consumer_definer",
        ):
            self.assertIn(role, BOOTSTRAP)
        self.assertIn("ALTER ROLE apdl_runtime WITH\n    LOGIN", BOOTSTRAP)
        self.assertIn("ALTER ROLE apdl_agents WITH\n    LOGIN", BOOTSTRAP)
        self.assertIn("ALTER ROLE apdl_llm_vault WITH\n    LOGIN", BOOTSTRAP)
        self.assertGreaterEqual(BOOTSTRAP.count("NOLOGIN"), 4)
        self.assertGreaterEqual(BOOTSTRAP.count("NOSUPERUSER"), 7)
        self.assertIn("BEGIN;", BOOTSTRAP)
        self.assertIn("COMMIT;", BOOTSTRAP)
        self.assertIn("pg_catalog.pg_auth_members", BOOTSTRAP)
        self.assertIn("pg_catalog.pg_shdepend", BOOTSTRAP)
        self.assertIn("WHERE roleid = role_record.oid", BOOTSTRAP)
        self.assertIn("WHERE member = role_record.oid", BOOTSTRAP)
        self.assertIn(
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
            BOOTSTRAP,
        )
        self.assertIn("pg_catalog.has_schema_privilege(", BOOTSTRAP)
        self.assertNotIn("SELECT 'REVOKE apdl_", BOOTSTRAP)

    def test_migration_enforces_one_canonical_purge_boundary(self) -> None:
        self.assertIn(
            "CREATE FUNCTION public.apdl_purge_experiment_audit(",
            MIGRATION,
        )
        self.assertNotIn(
            "CREATE OR REPLACE FUNCTION public.apdl_purge_experiment_audit(",
            MIGRATION,
        )
        self.assertIn("SET search_path = pg_catalog", MIGRATION)
        self.assertNotIn("SET search_path = pg_catalog, public", MIGRATION)
        self.assertIn(
            "procedure.proname = 'apdl_purge_experiment_audit'",
            MIGRATION,
        )
        self.assertIn("procedure.prosecdef", MIGRATION)
        self.assertIn("procedure.proconfig IS NOT DISTINCT FROM", MIGRATION)
        self.assertIn("pg_catalog.aclexplode(", MIGRATION)
        self.assertIn(
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
            MIGRATION,
        )
        self.assertIn(
            "GRANT USAGE ON SCHEMA public TO apdl_audit_operator",
            MIGRATION,
        )
        self.assertIn(
            "ON experiment_audit_log, experiment_audit_purge_log",
            MIGRATION,
        )

    def test_migration_rejects_contaminated_fixed_roles(self) -> None:
        self.assertGreaterEqual(
            MIGRATION.count("FROM pg_catalog.pg_auth_members"),
            3,
        )
        self.assertIn("WHERE member = role_record.oid", MIGRATION)
        self.assertIn("WHERE roleid = role_record.oid", MIGRATION)
        self.assertIn("FROM pg_catalog.pg_shdepend", MIGRATION)
        self.assertIn("FROM pg_catalog.pg_database", MIGRATION)
        self.assertIn("FROM pg_catalog.pg_namespace", MIGRATION)
        self.assertIn(
            "apdl_audit_purge_definer must not own preexisting database objects",
            MIGRATION,
        )

    def test_agents_capability_issuance_is_column_scoped(self) -> None:
        self.assertIn(
            """GRANT INSERT (
    token_hash,
    project_id,
    execution_kind,
    execution_id,
    run_id,
    execution_owner_id,
    audiences,
    roles,
    request_sha256,
    expires_at
) ON public.agent_service_capabilities TO apdl_agents;""",
            CAPABILITY_MIGRATION,
        )
        self.assertIn(
            """REVOKE INSERT, UPDATE, DELETE
    ON public.agent_service_capabilities
    FROM apdl_runtime;""",
            CAPABILITY_MIGRATION,
        )
        self.assertNotIn(
            "GRANT INSERT ON public.agent_service_capabilities TO apdl_agents",
            CAPABILITY_MIGRATION,
        )
        self.assertNotIn(
            "GRANT UPDATE ON public.agent_service_capabilities TO apdl_runtime",
            CAPABILITY_MIGRATION,
        )
        self.assertIn(
            "apdl_agents capability insert columns are not exact",
            PRIVILEGE_PROBE,
        )
        self.assertIn(
            "apdl_agents capability INSERT grant is not column-only",
            PRIVILEGE_PROBE,
        )

    def test_capability_consumer_is_a_narrow_definer_boundary(self) -> None:
        signature = """public.apdl_consume_agent_service_capability(
    UUID,
    TEXT,
    TEXT,
    TEXT,
    TEXT
)"""
        self.assertIn(
            f"ALTER FUNCTION {signature} OWNER TO",
            CAPABILITY_MIGRATION,
        )
        self.assertIn(
            "apdl_capability_consumer_definer;",
            CAPABILITY_MIGRATION,
        )
        self.assertIn(
            "GRANT SELECT ON public.agent_service_capabilities\n"
            "TO apdl_capability_consumer_definer;",
            CAPABILITY_MIGRATION,
        )
        self.assertIn(
            "GRANT UPDATE (consumed_at) ON public.agent_service_capabilities\n"
            "TO apdl_capability_consumer_definer;",
            CAPABILITY_MIGRATION,
        )
        self.assertIn(
            """REVOKE ALL ON FUNCTION public.apdl_consume_agent_service_capability(
    UUID,
    TEXT,
    TEXT,
    TEXT,
    TEXT
) FROM PUBLIC, apdl_agents, apdl_llm_vault;""",
            CAPABILITY_MIGRATION,
        )
        self.assertIn(
            """GRANT EXECUTE ON FUNCTION public.apdl_consume_agent_service_capability(
    UUID,
    TEXT,
    TEXT,
    TEXT,
    TEXT
) TO apdl_runtime;""",
            CAPABILITY_MIGRATION,
        )
        self.assertIn(
            "capability consume function ACL is not exact",
            PRIVILEGE_PROBE,
        )
        self.assertIn(
            "capability consumer table dependencies are not exact",
            PRIVILEGE_PROBE,
        )
        self.assertIn(
            'if [ "$first_consume|$second_consume|$consumed_state" '
            '!= "t|f|t" ]',
            PRIVILEGE_PROBE,
        )

    def test_project_authority_functions_are_owned_and_acl_scoped(self) -> None:
        for signature in (
            "apdl_project_management_authority(\n    TEXT,\n    UUID\n)",
            "apdl_agents_grant_owner_execution_roles(\n    TEXT,\n    UUID\n)",
            "apdl_assert_execution_project_authorized(\n    TEXT,\n    TEXT\n)",
        ):
            self.assertIn(
                f"ALTER FUNCTION public.{signature} OWNER TO "
                "apdl_project_authority_definer;",
                CAPABILITY_MIGRATION,
            )
        self.assertIn(
            "project authority function ownership is invalid",
            PRIVILEGE_PROBE,
        )
        self.assertIn(
            "project authority function ACLs are not exact",
            PRIVILEGE_PROBE,
        )

    def test_agents_database_mutation_surface_is_explicit(self) -> None:
        self.assertIn(
            "GRANT USAGE, SELECT ON\n"
            "    public.agent_audit_log_id_seq,\n"
            "    public.agent_memory_id_seq,\n"
            "    public.experiment_verdicts_id_seq\n"
            "TO apdl_agents;",
            CAPABILITY_MIGRATION,
        )
        self.assertNotIn(
            "ON ALL SEQUENCES IN SCHEMA public\n    TO apdl_agents",
            CAPABILITY_MIGRATION,
        )
        self.assertIn(
            "apdl_agents table DML allowlist is not exact",
            PRIVILEGE_PROBE,
        )
        self.assertIn(
            "apdl_agents sequence allowlist is not exact",
            PRIVILEGE_PROBE,
        )
        self.assertIn(
            "apdl_agents can access sensitive Admin or vault state",
            PRIVILEGE_PROBE,
        )
        self.assertIn(
            "'apdl_agents', 'public.admin_user_projects', 'UPDATE'",
            PRIVILEGE_PROBE,
        )
        self.assertIn(
            "'apdl_agents', 'public.admin_project_membership_audit', 'INSERT'",
            PRIVILEGE_PROBE,
        )

    def test_live_proof_runs_in_both_fresh_suites_and_agents_ci(self) -> None:
        self.assertIn("test_postgres_operator_privileges.sh", FRESH_SMOKE)
        self.assertIn("core|experiment", FRESH_SMOKE)
        self.assertIn("Bootstrap dedicated PostgreSQL roles", CI)
        self.assertIn(
            "Prove PostgreSQL runtime/operator privilege separation",
            CI,
        )
        self.assertIn(
            "APDL_RUNTIME_TEST_POSTGRES_URL: "
            "postgresql://apdl_runtime@127.0.0.1:5432/apdl",
            CI,
        )

    def test_live_proof_uses_the_strict_purge_contract(self) -> None:
        self.assertIn(
            "apdl_purge_experiment_audit(text,timestamptz,text,text)",
            PRIVILEGE_PROBE,
        )
        self.assertIn("actor = '$test_actor'", PRIVILEGE_PROBE)
        self.assertIn(
            "RAISE EXCEPTION 'forced post-purge transaction failure'",
            PRIVILEGE_PROBE,
        )
        self.assertNotIn("ROLLBACK;", PRIVILEGE_PROBE)
        self.assertIn("tgenabled = 'O'", PRIVILEGE_PROBE)
        self.assertIn("Unexpected apdl_runtime privilege posture", PRIVILEGE_PROBE)
        self.assertIn("pg_catalog.pg_auth_members", PRIVILEGE_PROBE)
        self.assertIn("pg_catalog.pg_shdepend", PRIVILEGE_PROBE)
        self.assertIn("pg_catalog.aclexplode(", PRIVILEGE_PROBE)
        self.assertIn(
            "the seven fixed APDL roles are not present",
            PRIVILEGE_PROBE,
        )
        self.assertIn(
            "Operator could not preview the bounded purge",
            PRIVILEGE_PROBE,
        )
        self.assertIn(
            'purge_state="$(\n    operator_psql -c',
            PRIVILEGE_PROBE,
        )
        self.assertIn("TRUNCATE public.experiment_audit_purge_log", PRIVILEGE_PROBE)
        self.assertNotIn("p_actor", PRIVILEGE_PROBE)


if __name__ == "__main__":
    unittest.main()
