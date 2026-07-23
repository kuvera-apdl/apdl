#!/usr/bin/env python3
"""Validate published image records and render the release runtime lock."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

if __package__:
    from .verify_release import ReleaseContractError, validate_manifest
else:
    from verify_release import ReleaseContractError, validate_manifest


IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
RECORD_KEYS = {"name", "repository", "digest", "tag", "version"}
CORE_COMPOSE_IMAGES = (
    "postgres-migrate",
    "ingestion",
    "config",
    "query",
    "clickhouse-writer",
    "admin-api",
    "admin",
)


class PublishedImageError(ValueError):
    """Published image records do not match the release manifest."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PublishedImageError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _load_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishedImageError(f"cannot read {path}: {exc}") from exc


def assemble_index(manifest_path: Path, records_dir: Path) -> dict[str, Any]:
    """Return a strict, deterministic index of the published image digests."""

    manifest = _load_json(manifest_path)
    try:
        version, tag = validate_manifest(manifest)
    except ReleaseContractError as exc:
        raise PublishedImageError(str(exc)) from exc

    expected = {
        image["name"]: image["repository"] for image in manifest["docker_images"]
    }
    record_paths = sorted(records_dir.glob("*.json"))
    if len(record_paths) != len(expected):
        raise PublishedImageError(
            "published image record count differs from the release manifest: "
            f"expected={len(expected)}, actual={len(record_paths)}"
        )

    by_name: dict[str, dict[str, str]] = {}
    for path in record_paths:
        record = _load_json(path)
        if not isinstance(record, dict) or set(record) != RECORD_KEYS:
            raise PublishedImageError(f"invalid published image record: {path.name}")
        if any(not isinstance(record[key], str) for key in RECORD_KEYS):
            raise PublishedImageError(
                f"published image record fields must be strings: {path.name}"
            )
        name = record["name"]
        if name in by_name:
            raise PublishedImageError(f"duplicate published image name: {name}")
        if name not in expected:
            raise PublishedImageError(f"unknown published image name: {name}")
        if record["repository"] != expected[name]:
            raise PublishedImageError(f"image repository differs from manifest: {name}")
        if IMAGE_DIGEST_RE.fullmatch(record["digest"]) is None:
            raise PublishedImageError(f"invalid image digest: {name}")
        if record["tag"] != tag:
            raise PublishedImageError(f"image tag differs from manifest: {name}")
        if record["version"] != version:
            raise PublishedImageError(f"image version differs from manifest: {name}")
        by_name[name] = {
            **record,
            "reference": f"{record['repository']}@{record['digest']}",
        }

    missing = sorted(set(expected) - set(by_name))
    if missing:
        raise PublishedImageError(f"missing published image records: {missing!r}")

    images = [by_name[name] for name in expected]
    return {
        "schema_version": 1,
        "version": version,
        "tag": tag,
        "images": images,
    }


def render_core_compose_override(index: dict[str, Any]) -> str:
    """Bind the supported core Compose services to immutable release digests."""

    images = index.get("images")
    if not isinstance(images, list):
        raise PublishedImageError("container image index is missing its images array")
    references: dict[str, str] = {}
    for image in images:
        if not isinstance(image, dict):
            raise PublishedImageError(
                "container image index contains a non-object image"
            )
        name = image.get("name")
        reference = image.get("reference")
        if not isinstance(name, str) or not isinstance(reference, str):
            raise PublishedImageError("container image index contains an invalid image")
        if name in references:
            raise PublishedImageError(f"duplicate indexed image name: {name}")
        references[name] = reference

    missing = [name for name in CORE_COMPOSE_IMAGES if name not in references]
    if missing:
        raise PublishedImageError(f"core release images are missing: {missing!r}")

    lines = ["services:"]
    for name in CORE_COMPOSE_IMAGES:
        lines.extend((f"  {name}:", f"    image: {references[name]}"))
    return "\n".join(lines) + "\n"


def write_outputs(
    index: dict[str, Any], *, index_path: Path, compose_override_path: Path
) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    compose_override_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    compose_override_path.write_text(
        render_core_compose_override(index),
        encoding="utf-8",
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--records-dir", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--compose-override", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        index = assemble_index(args.manifest, args.records_dir)
        write_outputs(
            index,
            index_path=args.index,
            compose_override_path=args.compose_override,
        )
    except PublishedImageError as exc:
        print(f"release image validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
