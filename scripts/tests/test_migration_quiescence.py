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
SPEC = importlib.util.spec_from_file_location("apdl_migration_quiescence", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
quiescence = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = quiescence
SPEC.loader.exec_module(quiescence)


class MigrationQuiescenceTests(unittest.TestCase):
    def test_supported_entrypoints_bind_checks_to_engine_confirmations(self) -> None:
        clickhouse_check = CLICKHOUSE_INIT.index("scripts/migration_quiescence.py")
        clickhouse_apply = CLICKHOUSE_INIT.index("pipeline/clickhouse/migrate.py")
        self.assertLess(clickhouse_check, clickhouse_apply)
        self.assertIn("--service clickhouse-writer", CLICKHOUSE_INIT)
        self.assertIn("APDL_CLICKHOUSE_WRITERS_QUIESCED=1", CLICKHOUSE_INIT)

        postgres_check = POSTGRES_INIT.index("scripts/migration_quiescence.py")
        postgres_apply = POSTGRES_INIT.index('build "$POSTGRES_MIGRATOR_SERVICE"')
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
        self.assertIn("APDL_POSTGRES_SERVICES_QUIESCED=1", POSTGRES_INIT)

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
