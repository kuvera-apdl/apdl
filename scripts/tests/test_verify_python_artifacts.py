from __future__ import annotations

import importlib.util
import unittest
from email.message import EmailMessage
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "verify_python_artifacts", ROOT / "scripts/verify_python_artifacts.py"
)
assert SPEC is not None and SPEC.loader is not None
verify_artifacts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_artifacts)


class PythonArtifactVerifierTests(unittest.TestCase):
    def test_rejects_parent_traversal_members(self) -> None:
        with self.assertRaisesRegex(
            verify_artifacts.ArtifactVerificationError, "unsafe member"
        ):
            verify_artifacts._safe_member_names(
                ["apdl_sdk-0.3.0/../outside"], "sdist"
            )

    def test_rejects_duplicate_archive_members(self) -> None:
        with self.assertRaisesRegex(
            verify_artifacts.ArtifactVerificationError, "duplicate member"
        ):
            verify_artifacts._safe_member_names(["apdl/__init__.py"] * 2, "wheel")

    def test_requires_pep_639_license_metadata(self) -> None:
        metadata = EmailMessage()
        metadata["Name"] = "apdl-sdk"
        metadata["Version"] = "0.3.0"
        metadata["License-File"] = "LICENSE"
        metadata["Requires-Python"] = ">=3.12"

        with self.assertRaisesRegex(
            verify_artifacts.ArtifactVerificationError, "license expression"
        ):
            verify_artifacts._verify_metadata(metadata, "0.3.0", "wheel")


if __name__ == "__main__":
    unittest.main()
