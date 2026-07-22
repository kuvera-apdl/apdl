"""Static contracts for the two isolated Compose smoke suites."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class FreshSmokeContractTests(unittest.TestCase):
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
        self.assertIn("smoke-fresh:\n\t@bash scripts/smoke_fresh_install.sh core", makefile)
        self.assertIn(
            "smoke-experiment-fresh:\n"
            "\t@bash scripts/smoke_fresh_install.sh experiment",
            makefile,
        )


if __name__ == "__main__":
    unittest.main()
