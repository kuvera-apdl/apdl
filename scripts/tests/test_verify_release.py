from __future__ import annotations

import copy
import importlib.util
import json
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
    "docker_images": copy.deepcopy(verify_release.EXPECTED_DOCKER_IMAGES),
}


class ReleaseManifestTests(unittest.TestCase):
    def test_checked_out_release_contract_is_consistent(self) -> None:
        version = verify_release.verify_release(ROOT, None, {})

        self.assertEqual(version, "0.3.0")

    def test_canonical_runtime_image_repositories_are_stable(self) -> None:
        repositories = {
            image["name"]: image["repository"]
            for image in VALID_MANIFEST["docker_images"]
        }

        self.assertEqual(
            repositories,
            {
                "admin": "ghcr.io/kuvera-apdl/apdl-admin",
                "admin-api": "ghcr.io/kuvera-apdl/apdl-admin-api",
                "agents": "ghcr.io/kuvera-apdl/apdl-agents",
                "clickhouse-writer": "ghcr.io/kuvera-apdl/apdl-clickhouse-writer",
                "codegen": "ghcr.io/kuvera-apdl/apdl-codegen",
                "codegen-egress": "ghcr.io/kuvera-apdl/apdl-codegen-egress",
                "codegen-worker": "ghcr.io/kuvera-apdl/apdl-codegen-worker",
                "config": "ghcr.io/kuvera-apdl/apdl-config",
                "ingestion": "ghcr.io/kuvera-apdl/apdl-ingestion",
                "postgres-migrate": "ghcr.io/kuvera-apdl/apdl-postgres-migrate",
                "query": "ghcr.io/kuvera-apdl/apdl-query",
            },
        )

    def test_manifest_requires_every_canonical_runtime_image(self) -> None:
        manifest = copy.deepcopy(VALID_MANIFEST)
        manifest["docker_images"].pop()

        with self.assertRaisesRegex(
            verify_release.ReleaseContractError, "canonical APDL runtime image set"
        ):
            verify_release.validate_manifest(manifest)

    def test_manifest_rejects_noncanonical_image_repository(self) -> None:
        manifest = copy.deepcopy(VALID_MANIFEST)
        manifest["docker_images"][0]["repository"] = "ghcr.io/example/admin"

        with self.assertRaisesRegex(
            verify_release.ReleaseContractError, "canonical APDL runtime image set"
        ):
            verify_release.validate_manifest(manifest)

    def test_manifest_rejects_reordered_images(self) -> None:
        manifest = copy.deepcopy(VALID_MANIFEST)
        manifest["docker_images"][0], manifest["docker_images"][1] = (
            manifest["docker_images"][1],
            manifest["docker_images"][0],
        )

        with self.assertRaisesRegex(
            verify_release.ReleaseContractError, "canonical APDL runtime image set"
        ):
            verify_release.validate_manifest(manifest)

    def test_manifest_rejects_unknown_image_fields(self) -> None:
        manifest = copy.deepcopy(VALID_MANIFEST)
        manifest["docker_images"][0]["tag"] = "latest"

        with self.assertRaisesRegex(
            verify_release.ReleaseContractError, r"unknown=\['tag'\]"
        ):
            verify_release.validate_manifest(manifest)

    def test_docker_matrix_binds_sensitive_build_inputs(self) -> None:
        revision = "a" * 40
        policy_digest = "b" * 64

        matrix = verify_release.render_docker_build_matrix(
            copy.deepcopy(VALID_MANIFEST),
            revision=revision,
            egress_policy_sha256=policy_digest,
        )

        self.assertEqual(len(matrix["include"]), 11)
        by_name = {image["name"]: image for image in matrix["include"]}
        self.assertEqual(
            by_name["codegen-worker"]["build_args"],
            f"CODEGEN_REVISION={revision}",
        )
        self.assertEqual(
            by_name["codegen-egress"]["build_args"],
            f"CODEGEN_EGRESS_POLICY_SHA256={policy_digest}",
        )
        self.assertEqual(by_name["agents"]["build_args"], "")

    def test_docker_matrix_requires_full_lowercase_digests(self) -> None:
        with self.assertRaisesRegex(
            verify_release.ReleaseContractError, "full lowercase Git SHA"
        ):
            verify_release.render_docker_build_matrix(
                copy.deepcopy(VALID_MANIFEST),
                revision="A" * 40,
                egress_policy_sha256="b" * 64,
            )
        with self.assertRaisesRegex(
            verify_release.ReleaseContractError, "lowercase SHA-256"
        ):
            verify_release.render_docker_build_matrix(
                copy.deepcopy(VALID_MANIFEST),
                revision="a" * 40,
                egress_policy_sha256="not-a-digest",
            )

    def test_cli_docker_matrix_is_machine_readable(self) -> None:
        # The release workflow writes this compact output directly to GITHUB_OUTPUT.
        manifest = copy.deepcopy(VALID_MANIFEST)
        matrix = verify_release.render_docker_build_matrix(
            manifest,
            revision="a" * 40,
            egress_policy_sha256="b" * 64,
        )

        payload = json.dumps(matrix, separators=(",", ":"), sort_keys=True)

        self.assertNotIn("\n", payload)
        self.assertEqual(json.loads(payload), matrix)

    def test_manifest_rejects_unknown_fields(self) -> None:
        manifest = copy.deepcopy(VALID_MANIFEST)
        manifest["channel"] = "preview"

        with self.assertRaisesRegex(
            verify_release.ReleaseContractError, r"unknown=\['channel'\]"
        ):
            verify_release.validate_manifest(manifest)

    def test_manifest_rejects_semver_build_metadata_that_is_not_an_oci_tag(
        self,
    ) -> None:
        manifest = copy.deepcopy(VALID_MANIFEST)
        manifest["version"] = "0.3.0+build.1"
        manifest["tag"] = "v0.3.0+build.1"

        with self.assertRaisesRegex(
            verify_release.ReleaseContractError, "valid OCI registry tags"
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

    def test_release_workflow_publishes_manifest_driven_multiarch_images(self) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

        self.assertIn(
            "matrix: ${{ fromJSON(needs.build-artifacts.outputs.docker_matrix) }}",
            workflow,
        )
        self.assertIn("uses: docker/build-push-action@v6", workflow)
        self.assertIn("platforms: linux/amd64,linux/arm64", workflow)
        self.assertIn("provenance: mode=max", workflow)
        self.assertIn("sbom: true", workflow)
        self.assertIn("uses: actions/attest-build-provenance@v3", workflow)
        self.assertIn("packages: write", workflow)
        self.assertIn("Check immutable GHCR publication state", workflow)
        self.assertIn("group: release-publication", workflow)
        self.assertIn("awk '$1 == \"Digest:\" { print $2; exit }'", workflow)
        self.assertNotIn(
            'sha256sum "${inspection_dir}/${output_name}.json"',
            workflow,
        )
        self.assertIn("--source-digest \"$GITHUB_SHA\"", workflow)
        self.assertIn("--source-ref \"$GITHUB_REF\"", workflow)
        self.assertIn("if: steps.registry.outputs.state == 'absent'", workflow)
        self.assertIn("docker buildx imagetools inspect", workflow)
        self.assertIn("GHCR package must be Public", workflow)
        self.assertIn("sha256sum release-manifest.json", workflow)
        self.assertIn("name: published-image-${{ matrix.name }}", workflow)
        self.assertIn("release-artifacts/container-images.json", workflow)
        self.assertIn("sha256sum container-images.json >> SHA256SUMS", workflow)
        self.assertIn(
            "needs: [build-artifacts, publish-npm, publish-pypi, publish-docker-images]",
            workflow,
        )


if __name__ == "__main__":
    unittest.main()
