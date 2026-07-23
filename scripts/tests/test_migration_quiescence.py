"""Behavioral tests for the Docker Compose migration quiescence gate."""

from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "migration_quiescence.py"
CLICKHOUSE_INIT = (ROOT / "scripts" / "init-clickhouse.sh").read_text()
POSTGRES_INIT = (ROOT / "scripts" / "init-postgres.sh").read_text()
MAKEFILE = (ROOT / "Makefile").read_text()
COMPOSE = (ROOT / "infra" / "docker" / "docker-compose.yml").read_text()
DEPS_COMPOSE = (
    ROOT / "infra" / "docker" / "docker-compose.deps.yml"
).read_text()
CLICKHOUSE_UPGRADE_COMPOSE = (
    ROOT / "scripts" / "fixtures" / "docker-compose.clickhouse-upgrade.yml"
).read_text()
POSTGRES_MIGRATOR = (ROOT / "pipeline" / "postgres" / "migrate.py").read_text()
CLICKHOUSE_MIGRATOR = (ROOT / "pipeline" / "clickhouse" / "migrate.py").read_text()
SERVICE_POOL_ENTRYPOINTS = tuple(
    (ROOT / path).read_text()
    for path in (
        "services/ingestion/app/main.py",
        "services/config/app/main.py",
        "services/query/app/main.py",
        "services/agents/app/main.py",
        "services/codegen/app/main.py",
        "services/admin-api/app/main.py",
    )
)
WRITER_ENTRYPOINT = (ROOT / "pipeline/redis/clickhouse_writer.py").read_text()
GRANT_ENTRYPOINT = (
    ROOT / "services/codegen/app/github/grant_cli.py"
).read_text()
ADMIN_PROVISION_ENTRYPOINT = (
    ROOT / "services/admin-api/scripts/create_admin_user.py"
).read_text()
DEV_CREDENTIAL_SQL = (ROOT / "scripts/provision-dev-credential.sql").read_text()
SPEC = importlib.util.spec_from_file_location("apdl_migration_quiescence", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
quiescence = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = quiescence
SPEC.loader.exec_module(quiescence)


class MigrationQuiescenceTests(unittest.TestCase):
    def test_supported_entrypoints_bind_checks_to_engine_confirmations(self) -> None:
        clickhouse_final_server = CLICKHOUSE_INIT.index("/proc/1/cmdline")
        clickhouse_check = CLICKHOUSE_INIT.index("scripts/migration_quiescence.py")
        clickhouse_apply = CLICKHOUSE_INIT.index("pipeline/clickhouse/migrate.py")
        self.assertLess(clickhouse_final_server, clickhouse_check)
        self.assertLess(clickhouse_check, clickhouse_apply)
        self.assertIn("clickhouse-server|*/clickhouse-server", CLICKHOUSE_INIT)
        self.assertIn("postgres|*/postgres", CLICKHOUSE_INIT)
        self.assertIn("/proc/1/cmdline", POSTGRES_INIT)
        self.assertIn("postgres|*/postgres", POSTGRES_INIT)
        for compose_source in (
            COMPOSE,
            DEPS_COMPOSE,
            CLICKHOUSE_UPGRADE_COMPOSE,
        ):
            self.assertIn("/proc/1/cmdline", compose_source)
            self.assertIn(
                "clickhouse-server|*/clickhouse-server",
                compose_source,
            )
            self.assertIn("postgres|*/postgres", compose_source)
        self.assertIn("--service clickhouse-writer", CLICKHOUSE_INIT)
        self.assertIn('POSTGRES_CONTAINER_ID="$postgres_container_id"', CLICKHOUSE_INIT)

        postgres_build = POSTGRES_INIT.index('build "$POSTGRES_MIGRATOR_SERVICE"')
        postgres_check = POSTGRES_INIT.index("scripts/migration_quiescence.py")
        postgres_apply = POSTGRES_INIT.index('run --rm --no-deps')
        self.assertLess(postgres_build, postgres_check)
        self.assertLess(postgres_check, postgres_apply)
        for service in (
            "ingestion",
            "config",
            "query",
            "agents",
            "codegen",
            "clickhouse-writer",
            "admin-api",
            "admin",
            "gateway",
        ):
            self.assertIn(f"--service {service}", POSTGRES_INIT)
        self.assertNotIn("SERVICES_QUIESCED", POSTGRES_INIT)
        self.assertNotIn("WRITERS_QUIESCED", CLICKHOUSE_INIT)

    def test_every_supported_database_runtime_holds_the_shared_inhibitor(self) -> None:
        for source in SERVICE_POOL_ENTRYPOINTS:
            self.assertIn("pg_advisory_lock_shared($1)", source)
            self.assertIn("MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084", source)
            self.assertIn("init=_acquire_maintenance_inhibitor", source)
            self.assertIn("reset=_reset_maintenance_inhibitor", source)
            self.assertIn("max_inactive_connection_lifetime=0", source)
            self.assertIn("maintenance_connection = await", source)
            self.assertIn("add_termination_listener", source)
            self.assertIn("_monitor_maintenance_inhibitor", source)
            self.assertIn("asyncio.wait_for", source)
            self.assertIn(
                "objid IN ($1::bigint::oid, $2::bigint::oid)", source
            )
            self.assertIn("os._exit(1)", source)
            acquire = source.index("async def _acquire_maintenance_inhibitor")
            primary = source.index("MAINTENANCE_INHIBITOR_LOCK_ID,", acquire)
            guard = source.index("MAINTENANCE_GUARD_LOCK_ID,", acquire)
            self.assertLess(primary, guard)
        self.assertIn("pg_advisory_lock_shared($1)", WRITER_ENTRYPOINT)
        self.assertIn("MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084", WRITER_ENTRYPOINT)
        self.assertIn(
            "maintenance_connection = await maintenance_pool.acquire()",
            WRITER_ENTRYPOINT,
        )
        self.assertIn("add_termination_listener", WRITER_ENTRYPOINT)
        self.assertIn("_monitor_maintenance_inhibitor", WRITER_ENTRYPOINT)
        self.assertIn("min_size=2", WRITER_ENTRYPOINT)
        # Two checked-out sessions hold the redundant maintenance locks; one
        # additional pooled session is reserved for completeness authority
        # transactions and must not weaken either inhibitor.
        self.assertIn("max_size=3", WRITER_ENTRYPOINT)
        self.assertIn("WRITER_SINGLETON_LOCK_ID = 4_158_044_085", WRITER_ENTRYPOINT)
        self.assertIn("pg_try_advisory_lock($1)", WRITER_ENTRYPOINT)
        self.assertIn("_heartbeat_writer_singleton", WRITER_ENTRYPOINT)
        self.assertIn("for inhibitor_index in range(2)", WRITER_ENTRYPOINT)
        self.assertIn("require_writer_singleton=inhibitor_index == 1", WRITER_ENTRYPOINT)
        self.assertIn("authority_pool=maintenance_pool", WRITER_ENTRYPOINT)
        self.assertIn("objsubid = 1", WRITER_ENTRYPOINT)
        self.assertIn("objid::bigint = ANY($1::bigint[])", WRITER_ENTRYPOINT)
        self.assertIn("await writer.stop(flush_buffer=False)", WRITER_ENTRYPOINT)
        self.assertIn("_accepting_inserts", WRITER_ENTRYPOINT)
        self.assertIn("_drain_inflight_insert", WRITER_ENTRYPOINT)
        self.assertIn("KILL QUERY WHERE query_id", WRITER_ENTRYPOINT)
        self.assertIn("system.processes", WRITER_ENTRYPOINT)
        self.assertIn("apdl-runtime-writer-", WRITER_ENTRYPOINT)
        self.assertIn("FROM apdl_maintenance_gate", WRITER_ENTRYPOINT)
        self.assertIn("authority = 'runtime-writes'", WRITER_ENTRYPOINT)
        self.assertIn("external_tables=", WRITER_ENTRYPOINT)
        self.assertIn("POSTGRES_URL:", COMPOSE[COMPOSE.index("clickhouse-writer:") :])
        self.assertIn("postgres-migrate:", COMPOSE[COMPOSE.index("clickhouse-writer:") :])

        for source in (GRANT_ENTRYPOINT, ADMIN_PROVISION_ENTRYPOINT):
            acquire = source.index("pg_advisory_lock_shared($1)")
            primary = source.index("MAINTENANCE_INHIBITOR_LOCK_ID", acquire)
            guard = source.index("MAINTENANCE_GUARD_LOCK_ID", primary)
            self.assertLess(primary, guard)

        primary_sql = DEV_CREDENTIAL_SQL.index(
            "pg_advisory_lock_shared(:maintenance_inhibitor_lock_id)"
        )
        guard_sql = DEV_CREDENTIAL_SQL.index(
            "pg_advisory_lock_shared(:maintenance_guard_lock_id)"
        )
        mutation = DEV_CREDENTIAL_SQL.index("INSERT INTO auth_credentials")
        self.assertLess(primary_sql, guard_sql)
        self.assertLess(guard_sql, mutation)
        self.assertIn(
            'DEV_CREDENTIAL_SQL="$ROOT_DIR/scripts/provision-dev-credential.sql"',
            POSTGRES_INIT,
        )

    def test_both_migrators_hold_the_same_exclusive_database_fence(self) -> None:
        for source in (POSTGRES_MIGRATOR, CLICKHOUSE_MIGRATOR):
            self.assertIn("MAINTENANCE_INHIBITOR_LOCK_ID = 4_158_044_083", source)
            self.assertIn("MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084", source)
            primary = source.index("acquire_owner(MAINTENANCE_INHIBITOR_LOCK_ID)")
            guard = source.index("acquire_owner(MAINTENANCE_GUARD_LOCK_ID)")
            self.assertLess(primary, guard)
            self.assertIn("with _maintenance_fence() as fence:", source)
            self.assertIn("fence.assert_held()", source)
            self.assertIn("_run_fenced_command", source)
            self.assertIn("_stop_operation", source)
            self.assertIn("objid = ({lock_id}::bigint)::oid", source)
        self.assertIn("pg_terminate_backend", POSTGRES_MIGRATOR)
        self.assertIn("pg_stat_activity", POSTGRES_MIGRATOR)
        self.assertIn("PGAPPNAME", POSTGRES_MIGRATOR)
        self.assertIn("system.processes", CLICKHOUSE_MIGRATOR)
        self.assertIn("on_started=await_client_handshake", CLICKHOUSE_MIGRATOR)
        self.assertIn("/proc/$1/cmdline", CLICKHOUSE_MIGRATOR)
        self.assertIn("ClickHouse container-side migration client", CLICKHOUSE_MIGRATOR)
        self.assertIn("MAINTENANCE_OWNER_TABLE = \"apdl_active_maintenance\"", CLICKHOUSE_MIGRATOR)
        self.assertIn("with _migration_lock(client.container_id, client.database):", CLICKHOUSE_MIGRATOR)
        owner = CLICKHOUSE_MIGRATOR.index("with _maintenance_owner(client, fence):")
        runtime_gate = CLICKHOUSE_MIGRATOR.index(
            "with _maintenance_writer_gate(client, fence):",
            owner,
        )
        migration = CLICKHOUSE_MIGRATOR.index("migrate(directory, client, fence)", runtime_gate)
        backfill = CLICKHOUSE_MIGRATOR.index(
            "apply_backfills(backfills, client, fence)",
            migration,
        )
        convergence = CLICKHOUSE_MIGRATOR.index(
            "_verify_schema_convergence(",
            backfill,
        )
        self.assertLess(owner, runtime_gate)
        self.assertLess(runtime_gate, migration)
        self.assertLess(migration, backfill)
        self.assertLess(backfill, convergence)
        self.assertIn('RUNTIME_QUERY_ID_PREFIX = "apdl-runtime-"', CLICKHOUSE_MIGRATOR)
        self.assertIn("startsWith(query_id", CLICKHOUSE_MIGRATOR)
        for source in (POSTGRES_MIGRATOR, CLICKHOUSE_MIGRATOR):
            self.assertIn("the maintenance guard and retrying", source)
            self.assertIn("signal.SIGTERM", source)
        self.assertIn(
            "apply_backfills(backfills, client, fence)", CLICKHOUSE_MIGRATOR
        )

    def test_full_stack_restart_drains_services_before_migrations(self) -> None:
        dev_core = MAKEFILE[MAKEFILE.index("dev-core:") : MAKEFILE.index("dev-all:")]
        stop = dev_core.index("stop -t 30")
        clickhouse_migration = dev_core.index("migrate-clickhouse")
        postgres_migration = dev_core.index("migrate-postgres")
        self.assertLess(stop, clickhouse_migration)
        self.assertLess(stop, postgres_migration)
        self.assertIn("clickhouse-writer", dev_core[stop:clickhouse_migration])
        self.assertIn("stop_grace_period: 30s", COMPOSE)

    def test_allows_only_when_forbidden_services_are_stopped(self) -> None:
        responses = iter(("apdl-project\n", "db-id\tpostgres\napi-id\tconfig\n"))
        with patch.object(
            quiescence,
            "_run",
            side_effect=lambda command: next(responses),
        ):
            quiescence.assert_services_stopped(
                "db-id",
                ("clickhouse-writer", "agents", "codegen"),
            )

    def test_refuses_active_services_and_reports_container_ids(self) -> None:
        responses = iter(
            (
                "apdl-project\n",
                "writer-id\tclickhouse-writer\nagent-id\tagents\n",
            )
        )
        with (
            patch.object(
                quiescence,
                "_run",
                side_effect=lambda command: next(responses),
            ),
            self.assertRaisesRegex(
                quiescence.QuiescenceError,
                r"clickhouse-writer \(writer-id\).*agents \(agent-id\)",
            ),
        ):
            quiescence.assert_services_stopped(
                "db-id",
                ("clickhouse-writer", "agents"),
            )

    def test_refuses_an_unlabeled_anchor_container(self) -> None:
        with (
            patch.object(quiescence, "_run", return_value="<no value>\n"),
            self.assertRaisesRegex(
                quiescence.QuiescenceError,
                "not owned by Docker Compose",
            ),
        ):
            quiescence.assert_services_stopped("db-id", ("agents",))

    def test_refuses_malformed_docker_state(self) -> None:
        responses = iter(("apdl-project\n", "missing-service-label\n"))
        with (
            patch.object(
                quiescence,
                "_run",
                side_effect=lambda command: next(responses),
            ),
            self.assertRaisesRegex(quiescence.QuiescenceError, "Could not parse"),
        ):
            quiescence.assert_services_stopped("db-id", ("agents",))

    def test_cli_fails_closed_for_active_service(self) -> None:
        responses = iter(("apdl-project\n", "agent-id\tagents\n"))
        stderr = io.StringIO()
        with (
            patch.object(
                quiescence,
                "_run",
                side_effect=lambda command: next(responses),
            ),
            redirect_stderr(stderr),
        ):
            result = quiescence.main(
                ("--anchor-container", "db-id", "--service", "agents")
            )

        self.assertEqual(result, 1)
        self.assertIn("Migration quiescence check failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
