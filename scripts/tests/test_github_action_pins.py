"""Tests for the immutable GitHub Action reference policy."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "scripts" / "check_github_action_pins.py"
SPEC = importlib.util.spec_from_file_location("check_github_action_pins", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
pins = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pins
SPEC.loader.exec_module(pins)


class GithubActionPinPolicyTests(unittest.TestCase):
    def _violations(self, workflow: str, *, action: bool = False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = (
                Path(".github/actions/example/action.yml")
                if action
                else Path(".github/workflows/test.yml")
            )
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_text(workflow, encoding="utf-8")
            return pins.find_violations(root)

    def test_repository_action_references_are_immutable(self):
        self.assertEqual(pins.find_violations(REPOSITORY_ROOT), ())

    def test_full_commit_local_and_digest_references_are_accepted(self):
        sha = "a" * 40
        digest = "b" * 64
        workflow = f"""
steps:
  - uses: actions/checkout@{sha} # v7.0.1
  - uses: ./.github/actions/local
  - uses: docker://example.invalid/tool@sha256:{digest}
"""
        self.assertEqual(self._violations(workflow), ())

    def test_mutable_tag_branch_and_short_sha_are_rejected(self):
        workflow = """
steps:
  - uses: actions/checkout@v7
  - uses: pypa/gh-action-pypi-publish@release/v1
  - uses: actions/setup-python@abcdef0 # v6.3.0
"""
        violations = self._violations(workflow)
        self.assertEqual(len(violations), 3)
        self.assertTrue(
            all("40-character commit SHA" in item.message for item in violations)
        )

    def test_sha_requires_a_reviewable_version_comment(self):
        workflow = f"""
steps:
  - uses: actions/checkout@{'a' * 40}
  - uses: actions/setup-python@{'b' * 40} # latest
"""
        violations = self._violations(workflow)
        self.assertEqual(len(violations), 2)
        self.assertTrue(
            all("version comment" in item.message for item in violations)
        )

    def test_dynamic_or_multivalue_uses_is_rejected(self):
        workflow = """
steps:
  - uses: ${{ matrix.action }}
  - uses: actions/checkout@v7 extra
"""
        violations = self._violations(workflow)
        self.assertEqual(len(violations), 2)

    def test_composite_action_yaml_is_scanned(self):
        workflow = """
runs:
  using: composite
  steps:
    - uses: actions/setup-node@v7
"""
        violations = self._violations(workflow, action=True)
        self.assertEqual(len(violations), 1)
        self.assertEqual(
            violations[0].path,
            Path(".github/actions/example/action.yml"),
        )


if __name__ == "__main__":
    unittest.main()
