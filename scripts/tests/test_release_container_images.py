from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import release_container_images


ROOT = Path(__file__).resolve().parents[2]


class PublishedContainerImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.manifest = json.loads(
            (ROOT / "release-manifest.json").read_text(encoding="utf-8")
        )
        self.manifest_path = self.root / "release-manifest.json"
        self.manifest_path.write_text(
            json.dumps(self.manifest),
            encoding="utf-8",
        )
        self.records_dir = self.root / "records"
        self.records_dir.mkdir()
        for index, image in enumerate(self.manifest["docker_images"]):
            record = {
                "name": image["name"],
                "repository": image["repository"],
                "digest": f"sha256:{index + 1:064x}",
                "tag": self.manifest["tag"],
                "version": self.manifest["version"],
            }
            (self.records_dir / f"{index:02d}-{image['name']}.json").write_text(
                json.dumps(record),
                encoding="utf-8",
            )

    def _record_path(self, name: str) -> Path:
        return next(self.records_dir.glob(f"*-{name}.json"))

    def test_index_is_manifest_ordered_and_digest_pinned(self) -> None:
        index = release_container_images.assemble_index(
            self.manifest_path,
            self.records_dir,
        )

        self.assertEqual(index["schema_version"], 1)
        self.assertEqual(index["version"], self.manifest["version"])
        self.assertEqual(
            [image["name"] for image in index["images"]],
            [image["name"] for image in self.manifest["docker_images"]],
        )
        for image in index["images"]:
            self.assertEqual(
                image["reference"],
                f"{image['repository']}@{image['digest']}",
            )

    def test_index_rejects_missing_records(self) -> None:
        self._record_path("query").unlink()

        with self.assertRaisesRegex(
            release_container_images.PublishedImageError,
            "record count differs",
        ):
            release_container_images.assemble_index(
                self.manifest_path,
                self.records_dir,
            )

    def test_index_rejects_duplicate_names(self) -> None:
        query_path = self._record_path("query")
        duplicate = json.loads(self._record_path("config").read_text(encoding="utf-8"))
        query_path.write_text(json.dumps(duplicate), encoding="utf-8")

        with self.assertRaisesRegex(
            release_container_images.PublishedImageError,
            "duplicate published image name: config",
        ):
            release_container_images.assemble_index(
                self.manifest_path,
                self.records_dir,
            )

    def test_index_rejects_mutable_or_malformed_digest(self) -> None:
        query_path = self._record_path("query")
        record = json.loads(query_path.read_text(encoding="utf-8"))
        record["digest"] = "latest"
        query_path.write_text(json.dumps(record), encoding="utf-8")

        with self.assertRaisesRegex(
            release_container_images.PublishedImageError,
            "invalid image digest: query",
        ):
            release_container_images.assemble_index(
                self.manifest_path,
                self.records_dir,
            )

    def test_compose_override_uses_every_compose_backed_digest_reference(self) -> None:
        index = release_container_images.assemble_index(
            self.manifest_path,
            self.records_dir,
        )

        override = release_container_images.render_compose_override(index)

        self.assertEqual(override.count("@sha256:"), 9)
        for name in release_container_images.COMPOSE_IMAGES:
            self.assertIn(f"  {name}:\n    image: ", override)
        self.assertIn("  agents:", override)
        self.assertIn("  codegen:", override)
        self.assertNotIn("  codegen-worker:", override)
        self.assertNotIn("  codegen-egress:", override)
        self.assertNotIn("build:", override)

    def test_smoke_evidence_merges_only_both_supported_platforms(self) -> None:
        index = release_container_images.assemble_index(
            self.manifest_path,
            self.records_dir,
        )
        evidence_paths = []
        for platform in release_container_images.SUPPORTED_PLATFORMS:
            path = self.root / f"{platform.replace('/', '-')}.json"
            release_container_images.write_smoke_evidence(
                index,
                platform=platform,
                evidence_path=path,
            )
            evidence_paths.append(path)

        tested = release_container_images.merge_smoke_evidence(evidence_paths)

        self.assertEqual(tested["schema_version"], 2)
        self.assertEqual(len(tested["images"]), 11)
        for image in tested["images"]:
            self.assertEqual(
                image["tested_platforms"],
                ["linux/amd64", "linux/arm64"],
            )

    def test_smoke_evidence_rejects_duplicate_platforms(self) -> None:
        index = release_container_images.assemble_index(
            self.manifest_path,
            self.records_dir,
        )
        first = self.root / "first.json"
        second = self.root / "second.json"
        for path in (first, second):
            release_container_images.write_smoke_evidence(
                index,
                platform="linux/amd64",
                evidence_path=path,
            )

        with self.assertRaisesRegex(
            release_container_images.PublishedImageError,
            "duplicate smoke platform",
        ):
            release_container_images.merge_smoke_evidence([first, second])

    def test_smoke_evidence_rejects_platform_image_drift(self) -> None:
        index = release_container_images.assemble_index(
            self.manifest_path,
            self.records_dir,
        )
        amd64 = self.root / "amd64.json"
        arm64 = self.root / "arm64.json"
        release_container_images.write_smoke_evidence(
            index,
            platform="linux/amd64",
            evidence_path=amd64,
        )
        release_container_images.write_smoke_evidence(
            index,
            platform="linux/arm64",
            evidence_path=arm64,
        )
        evidence = json.loads(arm64.read_text(encoding="utf-8"))
        evidence["images"][0]["digest"] = f"sha256:{99:064x}"
        evidence["images"][0]["reference"] = (
            f"{evidence['images'][0]['repository']}@"
            f"{evidence['images'][0]['digest']}"
        )
        arm64.write_text(json.dumps(evidence), encoding="utf-8")

        with self.assertRaisesRegex(
            release_container_images.PublishedImageError,
            "identities differ",
        ):
            release_container_images.merge_smoke_evidence([amd64, arm64])

    def test_cli_prepares_attests_and_merges_the_tested_index(self) -> None:
        prepared_index = self.root / "runtime" / "container-images.json"
        override = self.root / "runtime" / "compose.yml"
        evidence_dir = self.root / "evidence"
        tested_index = self.root / "tested" / "container-images.json"

        self.assertEqual(
            release_container_images.main(
                [
                    "prepare",
                    "--manifest",
                    str(self.manifest_path),
                    "--records-dir",
                    str(self.records_dir),
                    "--index",
                    str(prepared_index),
                    "--compose-override",
                    str(override),
                ]
            ),
            0,
        )
        for platform in release_container_images.SUPPORTED_PLATFORMS:
            self.assertEqual(
                release_container_images.main(
                    [
                        "attest",
                        "--index",
                        str(prepared_index),
                        "--platform",
                        platform,
                        "--evidence",
                        str(evidence_dir / f"{platform.replace('/', '-')}.json"),
                    ]
                ),
                0,
            )
        self.assertEqual(
            release_container_images.main(
                [
                    "merge",
                    "--evidence-dir",
                    str(evidence_dir),
                    "--index",
                    str(tested_index),
                ]
            ),
            0,
        )

        tested = json.loads(tested_index.read_text(encoding="utf-8"))
        self.assertEqual(tested["schema_version"], 2)
        self.assertEqual(
            tested["images"][0]["tested_platforms"],
            ["linux/amd64", "linux/arm64"],
        )

    def test_release_workflow_gates_on_all_images_for_both_architectures(
        self,
    ) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        smoke = (ROOT / "scripts/smoke_fresh_install.sh").read_text(encoding="utf-8")
        auxiliary = (
            ROOT / "scripts/smoke_published_auxiliary_images.sh"
        ).read_text(encoding="utf-8")
        postgres_init = (ROOT / "scripts/init-postgres.sh").read_text(encoding="utf-8")

        self.assertIn("smoke-published-images:", workflow)
        self.assertIn("runner: ubuntu-24.04-arm", workflow)
        self.assertIn("platform: linux/amd64", workflow)
        self.assertIn("platform: linux/arm64", workflow)
        self.assertIn("python scripts/release_container_images.py prepare", workflow)
        self.assertIn("python scripts/release_container_images.py attest", workflow)
        self.assertIn("python scripts/release_container_images.py merge", workflow)
        self.assertIn('APDL_SMOKE_NO_BUILD: "true"', workflow)
        self.assertIn("APDL_SMOKE_ALL_IMAGES=true make smoke-fresh", workflow)
        self.assertIn("docker logout ghcr.io", workflow)
        self.assertIn("make smoke-experiment-fresh", workflow)
        self.assertEqual(
            workflow.count("needs: [build-artifacts, smoke-published-images]"),
            2,
        )
        self.assertIn("      - smoke-published-images", workflow)
        self.assertIn("name: published-image-smoke-${{ matrix.arch }}", workflow)
        self.assertIn('case "${APDL_SMOKE_NO_BUILD:-false}"', smoke)
        self.assertIn('case "${APDL_SMOKE_ALL_IMAGES:-false}"', smoke)
        self.assertIn("startup_build_args=(--no-build)", smoke)
        self.assertIn("smoke_packaged_migrations=true", smoke)
        self.assertIn("smoke_published_auxiliary_images.sh", smoke)
        self.assertIn('"codegen-worker"', auxiliary)
        self.assertIn('"codegen-egress"', auxiliary)
        self.assertIn("--network none", auxiliary)
        self.assertIn("codegen-egress-healthcheck", auxiliary)
        self.assertIn('case "$POSTGRES_MIGRATOR_BUILD"', postgres_init)
        self.assertIn('case "$POSTGRES_USE_PACKAGED_MIGRATIONS"', postgres_init)


if __name__ == "__main__":
    unittest.main()
