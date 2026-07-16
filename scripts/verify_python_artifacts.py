#!/usr/bin/env python3
"""Verify the exact Python SDK wheel and sdist intended for PyPI."""

from __future__ import annotations

import argparse
import email.policy
import json
import sys
import tarfile
import zipfile
from email.message import Message
from pathlib import Path, PurePosixPath


class ArtifactVerificationError(ValueError):
    """A built Python artifact is missing or has unsafe/stale content."""


def _release_version(root: Path) -> str:
    try:
        manifest = json.loads((root / "release-manifest.json").read_text())
        version = manifest["version"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ArtifactVerificationError(f"cannot read release version: {exc}") from exc
    if not isinstance(version, str):
        raise ArtifactVerificationError("release version must be a string")
    return version


def _safe_member_names(names: list[str], label: str) -> set[str]:
    if len(names) != len(set(names)):
        raise ArtifactVerificationError(f"{label} contains duplicate member names")
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "" in path.parts:
            raise ArtifactVerificationError(f"{label} contains unsafe member {name!r}")
    forbidden = ("/.venv/", "/__pycache__/", "/.git/", "/.coverage")
    for name in names:
        normalized = f"/{name}"
        if any(part in normalized for part in forbidden) or name.endswith(".pyc"):
            raise ArtifactVerificationError(f"{label} contains development file {name!r}")
    return set(names)


def _metadata(data: bytes, label: str) -> Message:
    try:
        return email.message_from_bytes(data, policy=email.policy.default)
    except Exception as exc:  # email parsers expose several concrete errors
        raise ArtifactVerificationError(f"cannot parse {label} metadata: {exc}") from exc


def _verify_metadata(metadata: Message, version: str, label: str) -> None:
    if metadata.get("Name") != "apdl-sdk":
        raise ArtifactVerificationError(f"{label} distribution name is not apdl-sdk")
    if metadata.get("Version") != version:
        raise ArtifactVerificationError(
            f"{label} version {metadata.get('Version')!r} does not match {version!r}"
        )
    if metadata.get("License-Expression") != "MIT":
        raise ArtifactVerificationError(f"{label} is missing the MIT license expression")
    if metadata.get_all("License-File", []) != ["LICENSE"]:
        raise ArtifactVerificationError(f"{label} must declare exactly LICENSE")
    if metadata.get("Requires-Python") != ">=3.12":
        raise ArtifactVerificationError(f"{label} has stale Requires-Python metadata")


def _verify_wheel(path: Path, version: str, license_bytes: bytes) -> None:
    dist_info = f"apdl_sdk-{version}.dist-info"
    try:
        with zipfile.ZipFile(path) as archive:
            names = _safe_member_names(archive.namelist(), "wheel")
            required = {
                "apdl/__init__.py",
                "apdl/_version.py",
                "apdl/py.typed",
                f"{dist_info}/METADATA",
                f"{dist_info}/WHEEL",
                f"{dist_info}/RECORD",
                f"{dist_info}/licenses/LICENSE",
            }
            missing = sorted(required - names)
            if missing:
                raise ArtifactVerificationError(f"wheel is missing {missing!r}")
            _verify_metadata(
                _metadata(archive.read(f"{dist_info}/METADATA"), "wheel"),
                version,
                "wheel",
            )
            wheel_metadata = _metadata(
                archive.read(f"{dist_info}/WHEEL"), "wheel format"
            )
            if wheel_metadata.get("Root-Is-Purelib") != "true":
                raise ArtifactVerificationError("wheel must be a pure-Python wheel")
            if wheel_metadata.get_all("Tag", []) != ["py3-none-any"]:
                raise ArtifactVerificationError("wheel tag must be exactly py3-none-any")
            if archive.read(f"{dist_info}/licenses/LICENSE") != license_bytes:
                raise ArtifactVerificationError("wheel LICENSE differs from repository LICENSE")
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ArtifactVerificationError(f"cannot inspect wheel {path}: {exc}") from exc


def _verify_sdist(path: Path, version: str, license_bytes: bytes) -> None:
    prefix = f"apdl_sdk-{version}"
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = archive.getmembers()
            names = _safe_member_names([member.name for member in members], "sdist")
            for member in members:
                if not (member.isdir() or member.isfile()):
                    raise ArtifactVerificationError(
                        f"sdist contains unsupported member type {member.name!r}"
                    )
            required = {
                f"{prefix}/LICENSE",
                f"{prefix}/README.md",
                f"{prefix}/PKG-INFO",
                f"{prefix}/pyproject.toml",
                f"{prefix}/apdl/__init__.py",
                f"{prefix}/apdl/_version.py",
                f"{prefix}/apdl/py.typed",
            }
            missing = sorted(required - names)
            if missing:
                raise ArtifactVerificationError(f"sdist is missing {missing!r}")
            if any(not name.startswith(f"{prefix}/") and name != prefix for name in names):
                raise ArtifactVerificationError("sdist has more than one top-level directory")

            def read(name: str) -> bytes:
                stream = archive.extractfile(name)
                if stream is None:
                    raise ArtifactVerificationError(f"sdist member {name!r} is not a file")
                return stream.read()

            _verify_metadata(
                _metadata(read(f"{prefix}/PKG-INFO"), "sdist"), version, "sdist"
            )
            if read(f"{prefix}/LICENSE") != license_bytes:
                raise ArtifactVerificationError("sdist LICENSE differs from repository LICENSE")
    except (OSError, tarfile.TarError, KeyError) as exc:
        raise ArtifactVerificationError(f"cannot inspect sdist {path}: {exc}") from exc


def verify_artifacts(root: Path, dist_dir: Path, version: str) -> tuple[Path, Path]:
    expected_wheel = dist_dir / f"apdl_sdk-{version}-py3-none-any.whl"
    expected_sdist = dist_dir / f"apdl_sdk-{version}.tar.gz"
    try:
        actual = sorted(path.name for path in dist_dir.iterdir() if path.is_file())
    except OSError as exc:
        raise ArtifactVerificationError(f"cannot inspect artifact directory: {exc}") from exc
    expected = sorted([expected_wheel.name, expected_sdist.name])
    if actual != expected:
        raise ArtifactVerificationError(
            f"artifact directory must contain exactly {expected!r}, got {actual!r}"
        )
    try:
        license_bytes = (root / "LICENSE").read_bytes()
    except OSError as exc:
        raise ArtifactVerificationError(f"cannot read root LICENSE: {exc}") from exc
    _verify_wheel(expected_wheel, version, license_bytes)
    _verify_sdist(expected_sdist, version, license_bytes)
    return expected_wheel, expected_sdist


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist_dir", type=Path, help="directory containing the wheel and sdist")
    parser.add_argument("--version", help="expected version; defaults to release-manifest.json")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        version = args.version or _release_version(root)
        verify_artifacts(root, args.dist_dir.resolve(), version)
    except ArtifactVerificationError as exc:
        print(f"Python artifact verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"verified apdl-sdk {version} wheel and sdist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
