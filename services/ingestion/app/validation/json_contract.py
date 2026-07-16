"""Canonical JSON parsing and resource bounds for public event requests."""

from __future__ import annotations

import json
from typing import Any

MAX_REQUEST_BYTES = 512 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_JSON_DEPTH = 10
MAX_CONTAINER_ITEMS = 100
MAX_EVENT_NODES = 1_000


class CanonicalJSONError(ValueError):
    """Raised when JSON has ambiguous or non-canonical syntax/values."""


def parse_canonical_json(raw: bytes) -> object:
    """Parse strict JSON, rejecting duplicate keys and non-finite numbers."""

    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise CanonicalJSONError(f"Duplicate JSON object key: {key}")
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise CanonicalJSONError(f"Non-finite JSON number is not allowed: {value}")

    try:
        return json.loads(
            raw,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalJSONError("Invalid JSON in request body") from exc


def validate_event_json_bounds(event: object) -> None:
    """Enforce portable recursion, cardinality, node, and byte limits."""
    nodes = 0

    def visit(value: object, depth: int, path: str) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_EVENT_NODES:
            raise CanonicalJSONError(
                f"Event exceeds maximum JSON node count of {MAX_EVENT_NODES}"
            )
        if depth > MAX_JSON_DEPTH:
            raise CanonicalJSONError(
                f"{path} exceeds maximum JSON depth of {MAX_JSON_DEPTH}"
            )

        if isinstance(value, dict):
            if len(value) > MAX_CONTAINER_ITEMS:
                raise CanonicalJSONError(
                    f"{path} exceeds maximum object fields of {MAX_CONTAINER_ITEMS}"
                )
            for key, item in value.items():
                visit(item, depth + 1, f"{path}.{key}")
        elif isinstance(value, list):
            if len(value) > MAX_CONTAINER_ITEMS:
                raise CanonicalJSONError(
                    f"{path} exceeds maximum array items of {MAX_CONTAINER_ITEMS}"
                )
            for index, item in enumerate(value):
                visit(item, depth + 1, f"{path}[{index}]")

    visit(event, 0, "event")
    try:
        serialized = json.dumps(
            event,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CanonicalJSONError("Event contains a non-JSON value") from exc
    if len(serialized) > MAX_EVENT_BYTES:
        raise CanonicalJSONError(
            f"Event exceeds maximum serialized size of {MAX_EVENT_BYTES} bytes"
        )
