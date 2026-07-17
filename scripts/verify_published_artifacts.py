#!/usr/bin/env python3
"""Verify that immutable registry artifacts are absent or byte-identical."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


NPM_PACKAGE = "@apdl-oss/sdk"
PYPI_PACKAGE = "apdl-sdk"


class PublishedArtifactError(RuntimeError):
    """A registry artifact exists but does not match the release candidate."""


class ArtifactAbsent(LookupError):
    """The immutable version does not exist in the requested registry."""


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as artifact:
            for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PublishedArtifactError(f"cannot read artifact {path}: {exc}") from exc
    return digest.hexdigest()


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "apdl-release-artifact-verifier/1",
        },
    )


def _fetch_json(url: str) -> Any:
    try:
        with urllib.request.urlopen(_request(url), timeout=20) as response:
            content = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ArtifactAbsent(url) from exc
        raise PublishedArtifactError(
            f"registry request {url} returned HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PublishedArtifactError(f"registry request {url} failed: {exc}") from exc
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise PublishedArtifactError(f"registry response {url} was not JSON") from exc


def _download(url: str) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "registry.npmjs.org":
        raise PublishedArtifactError("npm tarball URL must use registry.npmjs.org HTTPS")
    try:
        with urllib.request.urlopen(_request(url), timeout=30) as response:
            return response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PublishedArtifactError(f"cannot download npm tarball: {exc}") from exc


def npm_artifact_state(
    version: str,
    artifact: Path,
    *,
    fetch_json: Callable[[str], Any] = _fetch_json,
    download: Callable[[str], bytes] = _download,
) -> str:
    """Return ``absent`` or ``identical`` for the canonical npm artifact."""

    package = urllib.parse.quote(NPM_PACKAGE, safe="")
    url = f"https://registry.npmjs.org/{package}/{version}"
    try:
        metadata = fetch_json(url)
    except ArtifactAbsent:
        return "absent"
    if not isinstance(metadata, dict):
        raise PublishedArtifactError("npm version metadata must be an object")
    if metadata.get("name") != NPM_PACKAGE or metadata.get("version") != version:
        raise PublishedArtifactError("npm version metadata identifies another artifact")
    dist = metadata.get("dist")
    if not isinstance(dist, dict) or not isinstance(dist.get("tarball"), str):
        raise PublishedArtifactError("npm version metadata has no canonical tarball URL")
    published_digest = _sha256_bytes(download(dist["tarball"]))
    candidate_digest = _sha256_file(artifact)
    if published_digest != candidate_digest:
        raise PublishedArtifactError(
            "npm version already exists with different tarball bytes: "
            f"published={published_digest} candidate={candidate_digest}"
        )
    return "identical"


def pypi_artifact_state(
    version: str,
    artifacts_dir: Path,
    *,
    fetch_json: Callable[[str], Any] = _fetch_json,
) -> str:
    """Return ``absent`` or ``identical`` for both canonical PyPI artifacts."""

    url = f"https://pypi.org/pypi/{PYPI_PACKAGE}/{version}/json"
    try:
        metadata = fetch_json(url)
    except ArtifactAbsent:
        return "absent"
    if not isinstance(metadata, dict):
        raise PublishedArtifactError("PyPI version metadata must be an object")
    info = metadata.get("info")
    if not isinstance(info, dict) or info.get("name") != PYPI_PACKAGE:
        raise PublishedArtifactError("PyPI version metadata identifies another project")
    if info.get("version") != version:
        raise PublishedArtifactError("PyPI version metadata identifies another version")
    urls = metadata.get("urls")
    if not isinstance(urls, list):
        raise PublishedArtifactError("PyPI version metadata has no artifact list")

    expected_names = {
        f"apdl_sdk-{version}-py3-none-any.whl",
        f"apdl_sdk-{version}.tar.gz",
    }
    published: dict[str, str] = {}
    for entry in urls:
        if not isinstance(entry, dict):
            raise PublishedArtifactError("PyPI artifact metadata must be an object")
        filename = entry.get("filename")
        digests = entry.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if not isinstance(filename, str) or not isinstance(digest, str):
            raise PublishedArtifactError("PyPI artifact metadata lacks filename/sha256")
        if filename in published:
            raise PublishedArtifactError(f"duplicate PyPI artifact metadata: {filename}")
        published[filename] = digest
    if set(published) != expected_names:
        raise PublishedArtifactError(
            "PyPI version artifact set differs: "
            f"expected={sorted(expected_names)!r} actual={sorted(published)!r}"
        )
    for filename in sorted(expected_names):
        candidate_digest = _sha256_file(artifacts_dir / filename)
        if published[filename] != candidate_digest:
            raise PublishedArtifactError(
                f"PyPI artifact {filename} differs: "
                f"published={published[filename]} candidate={candidate_digest}"
            )
    return "identical"


def _wait_for_identical(
    check: Callable[[], str],
    wait_seconds: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    deadline = monotonic() + wait_seconds
    while True:
        state = check()
        if state == "identical":
            return state
        if wait_seconds == 0 or monotonic() >= deadline:
            return state
        sleep(min(5.0, max(0.0, deadline - monotonic())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait-seconds", type=int, default=0)
    subparsers = parser.add_subparsers(dest="registry", required=True)

    npm_parser = subparsers.add_parser("npm")
    npm_parser.add_argument("--version", required=True)
    npm_parser.add_argument("--artifact", required=True, type=Path)

    pypi_parser = subparsers.add_parser("pypi")
    pypi_parser.add_argument("--version", required=True)
    pypi_parser.add_argument("--artifacts-dir", required=True, type=Path)

    args = parser.parse_args(argv)
    if args.wait_seconds < 0:
        parser.error("--wait-seconds must be non-negative")
    try:
        if args.registry == "npm":
            check = lambda: npm_artifact_state(args.version, args.artifact.resolve())
        else:
            check = lambda: pypi_artifact_state(
                args.version, args.artifacts_dir.resolve()
            )
        state = _wait_for_identical(check, args.wait_seconds)
        if args.wait_seconds and state != "identical":
            raise PublishedArtifactError(
                f"{args.registry} version remained absent after {args.wait_seconds}s"
            )
    except PublishedArtifactError as exc:
        print(f"published artifact verification failed: {exc}", file=sys.stderr)
        return 1
    print(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
