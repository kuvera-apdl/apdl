"""Non-configurable privacy safeguards for incoming analytics events."""

import math
import re
from collections.abc import Callable
from typing import Any

TAG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
MAX_CLICK_COORDINATE = 100_000
MIN_RAGE_CLICK_COUNT = 3
MAX_RAGE_CLICK_COUNT = 100


def _is_safe_tag(value: object) -> bool:
    return isinstance(value, str) and TAG_PATTERN.fullmatch(value) is not None


def _is_safe_coordinate(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= MAX_CLICK_COORDINATE
        and math.isfinite(value)
    )


def _is_safe_rage_click_count(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MIN_RAGE_CLICK_COUNT <= value <= MAX_RAGE_CLICK_COUNT
    )


AUTO_CAPTURE_PROPERTIES: dict[str, dict[str, Callable[[object], bool]]] = {
    "$click": {
        "tag": _is_safe_tag,
        "x": _is_safe_coordinate,
        "y": _is_safe_coordinate,
    },
    "$rage_click": {
        "tag": _is_safe_tag,
        "clickCount": _is_safe_rage_click_count,
        "x": _is_safe_coordinate,
        "y": _is_safe_coordinate,
    },
}


def _safe_properties(
    properties: object,
    schema: dict[str, Callable[[object], bool]],
) -> dict[str, Any]:
    if not isinstance(properties, dict):
        return {}

    return {
        key: properties[key]
        for key, validator in schema.items()
        if key in properties and validator(properties[key])
    }


def _safe_context(context: object) -> object:
    if not isinstance(context, dict):
        return context
    return {
        key: value
        for key, value in context.items()
        if key not in {"page", "referrer"}
    }


def sanitize_auto_capture_events(body: object) -> object:
    """Return ``body`` with reserved click events reduced to safe metadata.

    This permanent ingestion boundary protects against older JavaScript SDKs
    that sent DOM text, URLs, IDs, and classes. Copy-on-write preserves caller
    input and unrelated events while keeping legacy batches accepted.
    """
    if not isinstance(body, dict):
        return body

    events = body.get("events")
    if not isinstance(events, list):
        return body

    sanitized_events = events
    changed = False

    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        event_name = event.get("event")
        if not isinstance(event_name, str):
            continue
        property_schema = AUTO_CAPTURE_PROPERTIES.get(event_name)
        if property_schema is None:
            continue

        properties = event.get("properties")
        sanitized_properties = _safe_properties(properties, property_schema)
        context = event.get("context")
        sanitized_context = _safe_context(context)
        properties_changed = properties != sanitized_properties
        context_changed = context != sanitized_context
        if not properties_changed and not context_changed:
            continue

        if not changed:
            sanitized_events = list(events)
            changed = True

        sanitized_event = {**event, "properties": sanitized_properties}
        if context_changed:
            sanitized_event["context"] = sanitized_context
        sanitized_events[index] = sanitized_event

    if not changed:
        return body

    return {**body, "events": sanitized_events}
