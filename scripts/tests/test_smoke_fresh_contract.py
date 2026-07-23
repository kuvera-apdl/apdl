"""Static contracts for the two isolated Compose smoke suites."""

from pathlib import Path
import unittest
from datetime import datetime, timezone

from scripts import smoke_experiment_analysis


ROOT = Path(__file__).resolve().parents[2]


class FreshSmokeContractTests(unittest.TestCase):
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
