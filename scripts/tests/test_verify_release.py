from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "verify_release", ROOT / "scripts/verify_release.py"
)
assert SPEC is not None and SPEC.loader is not None
verify_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_release)


VALID_MANIFEST = {
    "schema_version": 1,
    "version": "0.3.0",
    "tag": "v0.3.0",
    "repository": "https://github.com/kuvera-apdl/apdl",
    "artifacts": {
        "source": {"provider": "github", "repository": "kuvera-apdl/apdl"},
        "npm": {"name": "@apdl-oss/sdk", "path": "sdk/javascript"},
        "pypi": {"name": "apdl-sdk", "path": "sdk/python"},
    },
    "docker_images": [],
}


class ReleaseManifestTests(unittest.TestCase):
    def test_checked_out_release_contract_is_consistent(self) -> None:
        version = verify_release.verify_release(ROOT, None, {})

        self.assertEqual(version, "0.3.0")

    def test_manifest_accepts_only_the_published_artifact_set(self) -> None:
        manifest = copy.deepcopy(VALID_MANIFEST)
        manifest["docker_images"] = ["ghcr.io/kuvera-apdl/apdl/query:v0.3.0"]

        with self.assertRaisesRegex(
            verify_release.ReleaseContractError, "docker_images must be empty"
        ):
            verify_release.validate_manifest(manifest)

    def test_manifest_rejects_unknown_fields(self) -> None:
        manifest = copy.deepcopy(VALID_MANIFEST)
        manifest["channel"] = "preview"

        with self.assertRaisesRegex(
            verify_release.ReleaseContractError, r"unknown=\['channel'\]"
        ):
            verify_release.validate_manifest(manifest)

    def test_tag_ref_must_match_manifest(self) -> None:
        with self.assertRaisesRegex(
            verify_release.ReleaseContractError, "does not match manifest tag"
        ):
            verify_release.verify_release(
                ROOT,
                None,
                {"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "v0.3.1"},
            )

    def test_branch_ref_does_not_require_a_tag(self) -> None:
        self.assertIsNone(
            verify_release.tag_from_environment(
                {"GITHUB_REF_TYPE": "branch", "GITHUB_REF_NAME": "main"}
            )
        )

if __name__ == "__main__":
    unittest.main()
