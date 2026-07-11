"""Strict JSON decoding shared by evaluator artifact boundaries."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path


def parse_strict_json_object(raw: str) -> dict:
    """Reject duplicate keys, non-finite constants, trailing data, and non-objects."""

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_nonfinite(value: str) -> None:
        raise ValueError(f"non-finite JSON value is forbidden: {value}")

    payload = json.loads(
        raw,
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_nonfinite,
    )
    if not isinstance(payload, dict):
        raise ValueError("JSON artifact must be an object")
    return payload


def read_bounded_regular_text(path: Path, *, max_bytes: int) -> str:
    """Read a regular non-symlink file without following a swapped symlink."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError("artifact path cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("artifact path must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("artifact path must be a regular file")
    if metadata.st_size > max_bytes:
        raise ValueError(f"artifact exceeds the {max_bytes}-byte size limit")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("artifact file could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("opened artifact is not a regular file")
        if opened.st_size > max_bytes:
            raise ValueError(f"artifact exceeds the {max_bytes}-byte size limit")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(65_536, max_bytes + 1 - total)):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"artifact exceeds the {max_bytes}-byte size limit")
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        return b"".join(chunks).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("artifact must be UTF-8 JSON") from exc
