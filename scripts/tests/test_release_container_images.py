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

    def test_compose_override_uses_only_exact_core_digest_references(self) -> None:
        index = release_container_images.assemble_index(
            self.manifest_path,
            self.records_dir,
        )

        override = release_container_images.render_core_compose_override(index)

        self.assertEqual(override.count("@sha256:"), 7)
        for name in release_container_images.CORE_COMPOSE_IMAGES:
            self.assertIn(f"  {name}:\n    image: ", override)
        self.assertNotIn("  agents:", override)
        self.assertNotIn("  codegen:", override)
        self.assertNotIn("build:", override)

    def test_release_workflow_gates_on_the_tested_image_index(self) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        smoke = (ROOT / "scripts/smoke_fresh_install.sh").read_text(encoding="utf-8")
        postgres_init = (ROOT / "scripts/init-postgres.sh").read_text(encoding="utf-8")

        self.assertIn("smoke-published-core:", workflow)
        self.assertIn("python scripts/release_container_images.py", workflow)
        self.assertIn('APDL_SMOKE_NO_BUILD: "true"', workflow)
        self.assertIn("docker logout ghcr.io", workflow)
        self.assertIn("make smoke-fresh\n          make smoke-experiment-fresh", workflow)
        self.assertEqual(
            workflow.count("needs: [build-artifacts, smoke-published-core]"),
            2,
        )
        self.assertIn("      - smoke-published-core", workflow)
        self.assertIn("name: tested-container-image-index", workflow)
        self.assertIn('case "${APDL_SMOKE_NO_BUILD:-false}"', smoke)
        self.assertIn("startup_build_args=(--no-build)", smoke)
        self.assertIn("smoke_packaged_migrations=true", smoke)
        self.assertIn('case "$POSTGRES_MIGRATOR_BUILD"', postgres_init)
        self.assertIn('case "$POSTGRES_USE_PACKAGED_MIGRATIONS"', postgres_init)


if __name__ == "__main__":
    unittest.main()
