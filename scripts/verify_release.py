#!/usr/bin/env python3
"""Verify the canonical APDL OSS release manifest and package versions."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


REPOSITORY_URL = "https://github.com/kuvera-apdl/apdl"
REPOSITORY_SLUG = "kuvera-apdl/apdl"
SEMVER_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OCI_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")

EXPECTED_DOCKER_IMAGES = [
    {
        "name": "admin",
        "repository": "ghcr.io/kuvera-apdl/apdl-admin",
        "context": "services/admin",
        "dockerfile": "services/admin/Dockerfile",
        "build_args": [],
    },
    {
        "name": "admin-api",
        "repository": "ghcr.io/kuvera-apdl/apdl-admin-api",
        "context": "services/admin-api",
        "dockerfile": "services/admin-api/Dockerfile",
        "build_args": [],
    },
    {
        "name": "agents",
        "repository": "ghcr.io/kuvera-apdl/apdl-agents",
        "context": "services/agents",
        "dockerfile": "services/agents/Dockerfile",
        "build_args": [],
    },
    {
        "name": "clickhouse-writer",
        "repository": "ghcr.io/kuvera-apdl/apdl-clickhouse-writer",
        "context": "pipeline/redis",
        "dockerfile": "pipeline/redis/Dockerfile",
        "build_args": [],
    },
    {
        "name": "codegen",
        "repository": "ghcr.io/kuvera-apdl/apdl-codegen",
        "context": "services/codegen",
        "dockerfile": "services/codegen/Dockerfile",
        "build_args": [],
    },
    {
        "name": "codegen-egress",
        "repository": "ghcr.io/kuvera-apdl/apdl-codegen-egress",
        "context": "infra/docker/codegen-egress",
        "dockerfile": "infra/docker/codegen-egress/Dockerfile",
        "build_args": ["CODEGEN_EGRESS_POLICY_SHA256"],
    },
    {
        "name": "codegen-worker",
        "repository": "ghcr.io/kuvera-apdl/apdl-codegen-worker",
        "context": "services/codegen",
        "dockerfile": "services/codegen/Dockerfile.worker",
        "build_args": ["CODEGEN_REVISION"],
    },
    {
        "name": "config",
        "repository": "ghcr.io/kuvera-apdl/apdl-config",
        "context": "services/config",
        "dockerfile": "services/config/Dockerfile",
        "build_args": [],
    },
    {
        "name": "ingestion",
        "repository": "ghcr.io/kuvera-apdl/apdl-ingestion",
        "context": "services/ingestion",
        "dockerfile": "services/ingestion/Dockerfile",
        "build_args": [],
    },
    {
        "name": "postgres-migrate",
        "repository": "ghcr.io/kuvera-apdl/apdl-postgres-migrate",
        "context": "pipeline/postgres",
        "dockerfile": "pipeline/postgres/Dockerfile",
        "build_args": [],
    },
    {
        "name": "query",
        "repository": "ghcr.io/kuvera-apdl/apdl-query",
        "context": "services/query",
        "dockerfile": "services/query/Dockerfile",
        "build_args": [],
    },
]


class ReleaseContractError(ValueError):
    """The checked-out revision does not satisfy the release contract."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseContractError(f"cannot read {path}: {exc}") from exc


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseContractError(f"cannot read {path}: {exc}") from exc


def _require_exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseContractError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ReleaseContractError(
            f"{label} keys differ: missing={missing!r}, unknown={unknown!r}"
        )
    return value


def validate_manifest(manifest: Any) -> tuple[str, str]:
    """Validate and return ``(version, tag)`` from the strict manifest."""

    manifest = _require_exact_keys(
        manifest,
        {
            "schema_version",
            "version",
            "tag",
            "repository",
            "artifacts",
            "docker_images",
        },
        "release manifest",
    )
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ReleaseContractError("release manifest schema_version must be integer 1")

    version = manifest["version"]
    if not isinstance(version, str) or SEMVER_RE.fullmatch(version) is None:
        raise ReleaseContractError("release manifest version must be canonical SemVer")
    tag = manifest["tag"]
    if tag != f"v{version}":
        raise ReleaseContractError(f"release manifest tag must be v{version}")
    if OCI_TAG_RE.fullmatch(version) is None or OCI_TAG_RE.fullmatch(tag) is None:
        raise ReleaseContractError(
            "release version and tag must also be valid OCI registry tags"
        )
    if manifest["repository"] != REPOSITORY_URL:
        raise ReleaseContractError(f"release repository must be {REPOSITORY_URL}")

    artifacts = _require_exact_keys(
        manifest["artifacts"], {"source", "npm", "pypi"}, "release artifacts"
    )
    expected_artifacts = {
        "source": {"provider": "github", "repository": REPOSITORY_SLUG},
        "npm": {"name": "@apdl-oss/sdk", "path": "sdk/javascript"},
        "pypi": {"name": "apdl-sdk", "path": "sdk/python"},
    }
    if artifacts != expected_artifacts:
        raise ReleaseContractError(
            "release artifacts must be exactly GitHub source, @apdl-oss/sdk, and apdl-sdk"
        )
    docker_images = manifest["docker_images"]
    if not isinstance(docker_images, list):
        raise ReleaseContractError("docker_images must be an array")
    for index, image in enumerate(docker_images):
        _require_exact_keys(
            image,
            {"name", "repository", "context", "dockerfile", "build_args"},
            f"docker_images[{index}]",
        )
    if docker_images != EXPECTED_DOCKER_IMAGES:
        raise ReleaseContractError(
            "docker_images must be exactly the canonical APDL runtime image set"
        )
    return version, tag


def render_docker_build_matrix(
    manifest: Any, *, revision: str, egress_policy_sha256: str
) -> dict[str, list[dict[str, str]]]:
    """Render validated manifest images into a GitHub Actions build matrix."""

    validate_manifest(manifest)
    if FULL_GIT_SHA_RE.fullmatch(revision) is None:
        raise ReleaseContractError(
            "Docker release revision must be a full lowercase Git SHA"
        )
    if SHA256_RE.fullmatch(egress_policy_sha256) is None:
        raise ReleaseContractError(
            "Codegen egress policy digest must be a lowercase SHA-256"
        )

    values = {
        "CODEGEN_REVISION": revision,
        "CODEGEN_EGRESS_POLICY_SHA256": egress_policy_sha256,
    }
    include: list[dict[str, str]] = []
    for image in manifest["docker_images"]:
        build_args = "\n".join(f"{name}={values[name]}" for name in image["build_args"])
        include.append(
            {
                "name": image["name"],
                "repository": image["repository"],
                "context": image["context"],
                "dockerfile": image["dockerfile"],
                "build_args": build_args,
            }
        )
    return {"include": include}


def _verify_docker_sources(root: Path, manifest: dict[str, Any]) -> None:
    manifested_dockerfiles = {
        image["dockerfile"] for image in manifest["docker_images"]
    }
    discovered_dockerfiles = {
        str(path.relative_to(root))
        for pattern in (
            "services/*/Dockerfile*",
            "pipeline/*/Dockerfile*",
            "infra/docker/*/Dockerfile*",
        )
        for path in root.glob(pattern)
        if path.is_file()
    }
    if manifested_dockerfiles != discovered_dockerfiles:
        missing = sorted(discovered_dockerfiles - manifested_dockerfiles)
        unknown = sorted(manifested_dockerfiles - discovered_dockerfiles)
        raise ReleaseContractError(
            "release manifest Dockerfiles differ from first-party image sources: "
            f"missing={missing!r}, unknown={unknown!r}"
        )

    for image in manifest["docker_images"]:
        context = root / image["context"]
        dockerfile = root / image["dockerfile"]
        if not context.is_dir():
            raise ReleaseContractError(
                f"Docker build context does not exist for {image['name']}: {image['context']}"
            )
        if not dockerfile.is_file():
            raise ReleaseContractError(
                f"Dockerfile does not exist for {image['name']}: {image['dockerfile']}"
            )
        try:
            dockerfile.resolve().relative_to(context.resolve())
        except ValueError as exc:
            raise ReleaseContractError(
                f"Dockerfile for {image['name']} must be inside its build context"
            ) from exc


def _read_python_version(path: Path) -> str:
    try:
        module = ast.parse(path.read_text(), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise ReleaseContractError(
            f"cannot parse Python version source {path}: {exc}"
        ) from exc

    values: list[str] = []
    for statement in module.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if isinstance(target, ast.Name) and target.id == "__version__":
            try:
                value = ast.literal_eval(statement.value)
            except (ValueError, TypeError) as exc:
                raise ReleaseContractError(
                    "Python __version__ must be a string literal"
                ) from exc
            if isinstance(value, str):
                values.append(value)
    if len(values) != 1:
        raise ReleaseContractError(
            "Python version source must define __version__ exactly once"
        )
    return values[0]


def _verify_npm(root: Path, version: str) -> None:
    package = _load_json(root / "sdk/javascript/package.json")
    if not isinstance(package, dict):
        raise ReleaseContractError("npm package must be an object")
    expected = {
        "name": "@apdl-oss/sdk",
        "version": version,
        "license": "MIT",
        "repository": {
            "type": "git",
            "url": f"git+{REPOSITORY_URL}.git",
            "directory": "sdk/javascript",
        },
        "homepage": f"{REPOSITORY_URL}/tree/main/sdk/javascript#readme",
        "bugs": {"url": f"{REPOSITORY_URL}/issues"},
        "files": ["dist"],
        "publishConfig": {"access": "public"},
    }
    for field, expected_value in expected.items():
        actual_value = package.get(field)
        if actual_value != expected_value:
            raise ReleaseContractError(
                f"npm package {field} must be {expected_value!r}, got {actual_value!r}"
            )

    lock = _load_json(root / "sdk/javascript/package-lock.json")
    if not isinstance(lock, dict):
        raise ReleaseContractError("npm lockfile must be an object")
    lock_packages = lock.get("packages")
    if not isinstance(lock_packages, dict):
        raise ReleaseContractError("npm lockfile is missing its packages object")
    lock_root = lock_packages.get("")
    if lock.get("name") != package["name"] or lock.get("version") != version:
        raise ReleaseContractError("npm lockfile top-level name/version is stale")
    if not isinstance(lock_root, dict):
        raise ReleaseContractError("npm lockfile is missing the root package")
    if lock_root.get("name") != package["name"] or lock_root.get("version") != version:
        raise ReleaseContractError("npm lockfile root package name/version is stale")


def _verify_python(root: Path, version: str) -> None:
    pyproject = _load_toml(root / "sdk/python/pyproject.toml")
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise ReleaseContractError("Python pyproject is missing [project]")
    if project.get("name") != "apdl-sdk":
        raise ReleaseContractError("Python distribution name must be apdl-sdk")
    if "version" in project or project.get("dynamic") != ["version"]:
        raise ReleaseContractError(
            "Python version must come only from the dynamic version source"
        )
    expected_urls = {
        "Homepage": f"{REPOSITORY_URL}/tree/main/sdk/python#readme",
        "Repository": REPOSITORY_URL,
        "Issues": f"{REPOSITORY_URL}/issues",
    }
    if project.get("urls") != expected_urls:
        raise ReleaseContractError(
            "Python project URLs do not use the canonical repository"
        )
    if project.get("license") != "MIT" or project.get("license-files") != ["LICENSE"]:
        raise ReleaseContractError(
            "Python project must use the MIT SPDX expression and package LICENSE"
        )

    dynamic = pyproject.get("tool", {}).get("setuptools", {}).get("dynamic", {})
    if dynamic.get("version") != {"attr": "apdl._version.__version__"}:
        raise ReleaseContractError(
            "Python dynamic version must use apdl._version.__version__"
        )
    source_version = _read_python_version(root / "sdk/python/apdl/_version.py")
    if source_version != version:
        raise ReleaseContractError(
            f"Python SDK version {source_version!r} does not match release {version!r}"
        )


def _verify_licenses(root: Path) -> None:
    try:
        canonical = (root / "LICENSE").read_bytes()
        package_licenses = {
            "npm": (root / "sdk/javascript/LICENSE").read_bytes(),
            "Python": (root / "sdk/python/LICENSE").read_bytes(),
        }
    except OSError as exc:
        raise ReleaseContractError(f"cannot read release license: {exc}") from exc
    for package, content in package_licenses.items():
        if content != canonical:
            raise ReleaseContractError(
                f"{package} package LICENSE differs from root LICENSE"
            )


def _verify_fixture_versions(root: Path, version: str) -> None:
    fixture = _load_json(root / "fixtures/events/canonical.json")
    if not isinstance(fixture, dict):
        raise ReleaseContractError("canonical event fixture must be an object")
    for event in fixture.get("valid", []):
        if not isinstance(event, dict):
            continue
        context = event.get("context")
        library = context.get("library") if isinstance(context, dict) else None
        if not isinstance(library, dict):
            continue
        if (
            library.get("name") in {"@apdl-oss/sdk", "apdl-python"}
            and library.get("version") != version
        ):
            raise ReleaseContractError(
                "canonical event fixture contains a stale SDK version"
            )


def tag_from_environment(environment: dict[str, str]) -> str | None:
    """Return the tag GitHub says is being built, or ``None`` off tag refs."""

    ref_type = environment.get("GITHUB_REF_TYPE")
    ref_name = environment.get("GITHUB_REF_NAME")
    ref = environment.get("GITHUB_REF")
    if ref_type == "tag":
        if not ref_name:
            raise ReleaseContractError("GITHUB_REF_TYPE=tag requires GITHUB_REF_NAME")
        return ref_name
    if ref and ref.startswith("refs/tags/"):
        return ref.removeprefix("refs/tags/")
    return None


def verify_release(
    root: Path, supplied_tag: str | None, environment: dict[str, str]
) -> str:
    manifest = _load_json(root / "release-manifest.json")
    version, manifest_tag = validate_manifest(manifest)
    active_tag = supplied_tag or tag_from_environment(environment)
    if active_tag is not None and active_tag != manifest_tag:
        raise ReleaseContractError(
            f"release tag {active_tag!r} does not match manifest tag {manifest_tag!r}"
        )

    _verify_npm(root, version)
    _verify_python(root, version)
    _verify_licenses(root)
    _verify_fixture_versions(root, version)
    _verify_docker_sources(root, manifest)
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        help="tag being released; optional off tag refs and strict when provided",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--docker-matrix",
        action="store_true",
        help="print the validated GitHub Actions Docker build matrix as JSON",
    )
    parser.add_argument(
        "--revision",
        help="full Git revision used by the Codegen worker image",
    )
    parser.add_argument(
        "--egress-policy-sha256",
        help="digest used by the Codegen egress policy image",
    )
    args = parser.parse_args(argv)
    try:
        version = verify_release(args.root.resolve(), args.tag, dict(os.environ))
        matrix = None
        if args.docker_matrix:
            if args.revision is None or args.egress_policy_sha256 is None:
                raise ReleaseContractError(
                    "--docker-matrix requires --revision and --egress-policy-sha256"
                )
            manifest = _load_json(args.root.resolve() / "release-manifest.json")
            matrix = render_docker_build_matrix(
                manifest,
                revision=args.revision,
                egress_policy_sha256=args.egress_policy_sha256,
            )
    except ReleaseContractError as exc:
        print(f"release verification failed: {exc}", file=sys.stderr)
        return 1
    if matrix is not None:
        print(json.dumps(matrix, separators=(",", ":"), sort_keys=True))
        return 0
    print(f"release contract verified for v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
