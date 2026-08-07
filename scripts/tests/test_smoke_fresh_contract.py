"""Static contracts for the two isolated Compose smoke suites."""

from pathlib import Path
import unittest
from datetime import datetime, timezone

from scripts import smoke_experiment_analysis


ROOT = Path(__file__).resolve().parents[2]


class FreshSmokeContractTests(unittest.TestCase):
    def test_disabled_codegen_polling_has_webhook_recovery_fixture(self) -> None:
        script = (ROOT / "scripts" / "smoke_fresh_install.sh").read_text()

        self.assertRegex(
            script,
            r'export GITHUB_WEBHOOK_SECRET="[A-Za-z0-9_-]{32,128}"',
        )
        self.assertLess(
            script.index("export GITHUB_WEBHOOK_SECRET="),
            script.index("export CODEGEN_CI_POLL_INTERVAL=0"),
        )

    def test_empty_bootstrap_precedes_explicit_smoke_fixture_seed(self) -> None:
        script = (ROOT / "scripts" / "smoke_fresh_install.sh").read_text()
        postgres_init = (ROOT / "scripts" / "init-postgres.sh").read_text()
        fixture = (
            ROOT / "scripts" / "fixtures" / "provision-smoke-credential.sql"
        ).read_text()
        dev = (ROOT / "scripts" / "dev.sh").read_text()
        core = (ROOT / "scripts" / "smoke_core.py").read_text()
        experiment = (
            ROOT / "scripts" / "smoke_experiment_analysis.py"
        ).read_text()

        self.assertNotIn("auth_credentials", postgres_init)
        self.assertNotIn("APDL_DEV_", postgres_init)
        self.assertNotIn("APDL_SMOKE_", postgres_init)
        self.assertIn(
            'SMOKE_CREDENTIAL_SQL="$ROOT_DIR/scripts/fixtures/'
            'provision-smoke-credential.sql"',
            script,
        )
        self.assertIn("INSERT INTO auth_credentials", fixture)

        migration = script.index('"$ROOT_DIR/scripts/init-postgres.sh"')
        empty_assertion = script.index("assert_empty_bootstrap_catalogs", migration)
        seed = script.index("seed_smoke_credentials", empty_assertion)
        credential_assertion = script.index("assert_credentials", seed)
        self.assertLess(migration, empty_assertion)
        self.assertLess(empty_assertion, seed)
        self.assertLess(seed, credential_assertion)
        for catalog in (
            "admin_projects",
            "admin_users",
            "admin_user_projects",
            "auth_credentials",
            "admin_managed_credentials",
            "llm_project_policies",
            "llm_project_provider_policies",
            "admin_project_execution_authorizations",
        ):
            self.assertIn(catalog, script)

        for source in (script, dev, core, experiment):
            self.assertNotIn("APDL_DEV_API_KEY", source)
            self.assertNotIn("APDL_DEV_CLIENT_KEY", source)
        for source in (script, dev, core):
            self.assertIn("APDL_SMOKE_CONFIDENTIAL_KEY", source)
            self.assertIn("APDL_SMOKE_BROWSER_KEY", source)
        self.assertIn("APDL_SMOKE_CONFIDENTIAL_KEY", experiment)
        self.assertIn(
            "scripts/dev.sh smoke-fresh Isolated end-to-end fresh-install proof",
            dev,
        )
        self.assertNotIn(
            'echo "  scripts/dev.sh smoke       End-to-end smoke test"',
            dev,
        )

    def test_experiment_projection_requires_frozen_enrollment_authority(self) -> None:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 7, 2, tzinfo=timezone.utc)
        contract = {
            "control_variant": "control",
            "variants": ["control", "treatment"],
            "metric_event": "purchase",
            "metric_direction": "increase",
            "enrollment_mode": "all",
            "minimum_exposure_config_version": 3,
            "statistical_plan": {"protocol": "fixed"},
        }
        projection = {
            "key": "experiment",
            "flag_key": "flag",
            "status": "completed",
            "control_variant": "control",
            "variants": ["control", "treatment"],
            "metric_event": "purchase",
            "metric_direction": "increase",
            "enrollment_mode": "all",
            "minimum_exposure_config_version": 3,
            "statistical_plan": {"protocol": "fixed"},
            "start_date": "2026-07-01T00:00:00Z",
            "end_date": "2026-07-02T00:00:00Z",
            "version": 7,
        }

        smoke_experiment_analysis._assert_projection(
            projection,
            experiment_key="experiment",
            flag_key="flag",
            contract=contract,
            start=start,
            end=end,
            version=7,
            expected_status="completed",
        )

    def test_core_and_experiment_suites_are_separate(self) -> None:
        script = (ROOT / "scripts" / "smoke_fresh_install.sh").read_text()
        makefile = (ROOT / "Makefile").read_text()

        self.assertIn("core|experiment", script)
        self.assertIn('if [ "$SMOKE_SUITE" = "core" ]', script)
        self.assertEqual(script.count('scripts/smoke_core.py"'), 1)
        self.assertEqual(script.count('scripts/smoke_experiment_analysis.py"'), 1)
        self.assertIn("test_postgres_fence_owner_loss.py", script)
        fence_probe = (
            ROOT / "scripts" / "test_postgres_fence_owner_loss.py"
        ).read_text()
        self.assertIn("pg_terminate_backend", fence_probe)
        self.assertIn("SELECT pg_sleep(30)", fence_probe)
        self.assertIn('if count != "0"', fence_probe)
        experiment_smoke = (
            ROOT / "scripts" / "smoke_experiment_analysis.py"
        ).read_text()
        self.assertNotIn("ALTER TABLE", experiment_smoke)
        self.assertNotIn("mutations_sync", experiment_smoke)
        self.assertIn(
            '_assert_equal(deleted["deleted"], False, "launched experiment deletion")',
            experiment_smoke,
        )
        self.assertIn(
            '_assert_equal(deleted["archived"], True, "launched experiment archive")',
            experiment_smoke,
        )
        self.assertNotIn("expected_status={404}", experiment_smoke)
        self.assertIn("archived_projection", experiment_smoke)
        self.assertIn("archived_analysis", experiment_smoke)
        self.assertIn("smoke-fresh:\n\t@bash scripts/smoke_fresh_install.sh core", makefile)
        self.assertIn(
            "smoke-experiment-fresh:\n"
            "\t@bash scripts/smoke_fresh_install.sh experiment",
            makefile,
        )


if __name__ == "__main__":
    unittest.main()
