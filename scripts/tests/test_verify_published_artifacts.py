from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "verify_published_artifacts.py"
SPEC = importlib.util.spec_from_file_location("verify_published_artifacts", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class PublishedArtifactTests(unittest.TestCase):
    def test_npm_absent_is_resumable_state(self) -> None:
        def absent(_: str):
            raise verify.ArtifactAbsent

        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "candidate.tgz"
            artifact.write_bytes(b"candidate")
            self.assertEqual(
                verify.npm_artifact_state("0.3.0", artifact, fetch_json=absent),
                "absent",
            )

    def test_npm_existing_identical_artifact_is_success(self) -> None:
        content = b"exact npm bytes"
        metadata = {
            "name": "@apdl-oss/sdk",
            "version": "0.3.0",
            "dist": {"tarball": "https://registry.npmjs.org/exact.tgz"},
        }
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "candidate.tgz"
            artifact.write_bytes(content)
            self.assertEqual(
                verify.npm_artifact_state(
                    "0.3.0",
                    artifact,
                    fetch_json=lambda _: metadata,
                    download=lambda _: content,
                ),
                "identical",
            )

    def test_npm_existing_different_artifact_fails_closed(self) -> None:
        metadata = {
            "name": "@apdl-oss/sdk",
            "version": "0.3.0",
            "dist": {"tarball": "https://registry.npmjs.org/different.tgz"},
        }
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "candidate.tgz"
            artifact.write_bytes(b"candidate")
            with self.assertRaisesRegex(
                verify.PublishedArtifactError, "different tarball bytes"
            ):
                verify.npm_artifact_state(
                    "0.3.0",
                    artifact,
                    fetch_json=lambda _: metadata,
                    download=lambda _: b"published",
                )

    def test_pypi_existing_identical_artifacts_are_success(self) -> None:
        version = "0.3.0"
        files = {
            f"apdl_sdk-{version}-py3-none-any.whl": b"wheel",
            f"apdl_sdk-{version}.tar.gz": b"sdist",
        }
        metadata = {
            "info": {"name": "apdl-sdk", "version": version},
            "urls": [
                {
                    "filename": filename,
                    "digests": {"sha256": hashlib.sha256(content).hexdigest()},
                }
                for filename, content in files.items()
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename, content in files.items():
                (root / filename).write_bytes(content)
            self.assertEqual(
                verify.pypi_artifact_state(
                    version, root, fetch_json=lambda _: metadata
                ),
                "identical",
            )

    def test_pypi_existing_partial_artifact_set_fails_closed(self) -> None:
        metadata = {
            "info": {"name": "apdl-sdk", "version": "0.3.0"},
            "urls": [
                {
                    "filename": "apdl_sdk-0.3.0.tar.gz",
                    "digests": {"sha256": "0" * 64},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                verify.PublishedArtifactError, "artifact set differs"
            ):
                verify.pypi_artifact_state(
                    "0.3.0", Path(directory), fetch_json=lambda _: metadata
                )

    def test_wait_retries_absent_until_registry_is_identical(self) -> None:
        states = iter(["absent", "absent", "identical"])
        clock = iter([0.0, 0.0, 1.0, 2.0, 3.0])
        sleeps: list[float] = []

        result = verify._wait_for_identical(
            lambda: next(states),
            10,
            sleep=sleeps.append,
            monotonic=lambda: next(clock),
        )

        self.assertEqual(result, "identical")
        self.assertEqual(len(sleeps), 2)


if __name__ == "__main__":
    unittest.main()
