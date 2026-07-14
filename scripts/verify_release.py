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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


REPOSITORY_URL = "https://github.com/kuvera-apdl/apdl"
REPOSITORY_SLUG = "kuvera-apdl/apdl"
SEMVER_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


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
        {"schema_version", "version", "tag", "repository", "artifacts", "docker_images"},
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
    if manifest["docker_images"] != []:
        raise ReleaseContractError("docker_images must be empty for the developer preview")
    return version, tag


def _read_python_version(path: Path) -> str:
    try:
        module = ast.parse(path.read_text(), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise ReleaseContractError(f"cannot parse Python version source {path}: {exc}") from exc

    values: list[str] = []
    for statement in module.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if isinstance(target, ast.Name) and target.id == "__version__":
            try:
                value = ast.literal_eval(statement.value)
            except (ValueError, TypeError) as exc:
                raise ReleaseContractError("Python __version__ must be a string literal") from exc
            if isinstance(value, str):
                values.append(value)
    if len(values) != 1:
        raise ReleaseContractError("Python version source must define __version__ exactly once")
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
        raise ReleaseContractError("Python version must come only from the dynamic version source")
    expected_urls = {
        "Homepage": f"{REPOSITORY_URL}/tree/main/sdk/python#readme",
        "Repository": REPOSITORY_URL,
        "Issues": f"{REPOSITORY_URL}/issues",
    }
    if project.get("urls") != expected_urls:
        raise ReleaseContractError("Python project URLs do not use the canonical repository")
    if project.get("license") != "MIT" or project.get("license-files") != ["LICENSE"]:
        raise ReleaseContractError(
            "Python project must use the MIT SPDX expression and package LICENSE"
        )

    dynamic = pyproject.get("tool", {}).get("setuptools", {}).get("dynamic", {})
    if dynamic.get("version") != {"attr": "apdl._version.__version__"}:
        raise ReleaseContractError("Python dynamic version must use apdl._version.__version__")
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
            raise ReleaseContractError(f"{package} package LICENSE differs from root LICENSE")


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
        if library.get("name") in {"@apdl-oss/sdk", "apdl-python"} and library.get(
            "version"
        ) != version:
            raise ReleaseContractError("canonical event fixture contains a stale SDK version")


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


def verify_release(root: Path, supplied_tag: str | None, environment: dict[str, str]) -> str:
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
    return version


def _assert_registry_version_absent(url: str, label: str) -> None:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "apdl-release-verifier/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return
        raise ReleaseContractError(
            f"cannot verify {label} availability: registry returned HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReleaseContractError(f"cannot verify {label} availability: {exc}") from exc
    if status == 200:
        raise ReleaseContractError(f"{label} already exists; refusing to overwrite a release")
    raise ReleaseContractError(
        f"cannot verify {label} availability: registry returned HTTP {status}"
    )


def verify_registry_versions_available(version: str) -> None:
    """Fail closed unless both immutable package versions are unpublished."""

    npm_name = urllib.parse.quote("@apdl-oss/sdk", safe="")
    _assert_registry_version_absent(
        f"https://registry.npmjs.org/{npm_name}/{version}",
        f"@apdl-oss/sdk@{version}",
    )
    _assert_registry_version_absent(
        f"https://pypi.org/pypi/apdl-sdk/{version}/json",
        f"apdl-sdk=={version}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        help="tag being released; optional off tag refs and strict when provided",
    )
    parser.add_argument(
        "--check-registries",
        action="store_true",
        help="fail unless both package versions are still available to publish",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    try:
        version = verify_release(args.root.resolve(), args.tag, dict(os.environ))
        if args.check_registries:
            verify_registry_versions_available(version)
    except ReleaseContractError as exc:
        print(f"release verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"release contract verified for v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
