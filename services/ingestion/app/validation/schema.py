"""Event batch validation.

Returns the format: ``{"valid": bool, "errors": [{"field": str, "message": str}]}``.

Cross-field checks (event-name-or-type, user/anon identifier, batch size) live
here rather than as model validators so that each failure is attributed to the
specific field a client would expect, and so that multiple errors per event are
collected in a single response instead of short-circuiting on the first.
"""

import math

from pydantic import ValidationError

from app.models.schemas import MAX_BATCH_SIZE, Event

FEATURE_FLAG_EXPOSURE_EVENT = "$feature_flag_exposure"
FRONTEND_ERROR_EVENT = "$frontend_error"
WEB_VITAL_EVENT = "$web_vital"
FEATURE_FLAG_EXPOSURE_ENVELOPE_KEYS = frozenset({
    "event",
    "type",
    "user_id",
    "anonymous_id",
    "group_id",
    "timestamp",
    "properties",
    "context",
    "message_id",
    "session_id",
})
FEATURE_FLAG_EXPOSURE_REQUIRED_ENVELOPE_KEYS = frozenset({
    "event",
    "type",
    "timestamp",
    "properties",
    "message_id",
    "session_id",
})
FEATURE_FLAG_EXPOSURE_KEYS = frozenset({
    "flag_key",
    "value",
    "reason",
    "rule_id",
    "bucket",
    "rollout_percentage",
    "bucket_by",
    "config_version",
    "source",
    "page",
})
FEATURE_FLAG_EXPOSURE_REASONS = frozenset({
    "invalid_config",
    "disabled",
    "error",
    "rule_match",
    "rule_rollout",
    "fallthrough",
    "fallthrough_rollout",
})
FEATURE_FLAG_EXPOSURE_SOURCES = frozenset({
    "memory",
    "initial_fetch",
    "sse",
    "local_storage",
    "server",
})
FRONTEND_ERROR_KEYS = frozenset({
    "error_type",
    "message",
    "page",
    "component",
    "slot_id",
    "source",
    "line",
    "column",
    "stack",
    "active_flags",
    "active_flag_versions",
})
FRONTEND_ERROR_TYPES = frozenset({
    "javascript_error",
    "unhandled_rejection",
    "component_render_error",
})
WEB_VITAL_KEYS = frozenset({
    "metric",
    "value",
    "rating",
    "delta",
    "id",
    "navigation_type",
    "page",
    "active_flags",
    "active_flag_versions",
})
WEB_VITAL_METRICS = frozenset({"CLS", "INP", "LCP"})
WEB_VITAL_RATINGS = frozenset({"good", "needs_improvement", "poor"})


def validate_event_batch(body: object) -> dict:
    """Validate a full event batch payload."""
    if not isinstance(body, dict):
        return _error("body", "Request body must be a JSON object")

    if "events" not in body:
        return _error("events", "Missing required field 'events'")

    events = body["events"]
    if not isinstance(events, list):
        return _error("events", "Field 'events' must be an array")

    if len(events) == 0:
        return _error("events", "Batch must contain at least one event")

    if len(events) > MAX_BATCH_SIZE:
        return _error("events", f"Batch size exceeds maximum of {MAX_BATCH_SIZE}")

    all_errors: list[dict] = []
    for i, ev in enumerate(events):
        all_errors.extend(_validate_event(ev, prefix=f"events[{i}]"))

    if all_errors:
        return {"valid": False, "errors": all_errors}
    return {"valid": True, "errors": []}


def validate_single_event(event: object) -> dict:
    """Validate one event as if it were standalone (not wrapped in a batch)."""
    errors = _validate_event(event, prefix="")
    if errors:
        return {"valid": False, "errors": errors}
    return {"valid": True, "errors": []}


def _validate_event(event: object, prefix: str) -> list[dict]:
    if not isinstance(event, dict):
        return [{"field": prefix or "event", "message": "Event must be an object"}]

    errors: list[dict] = []

    has_event_name = isinstance(event.get("event"), str) and bool(event["event"])
    has_type = isinstance(event.get("type"), str) and bool(event["type"])
    if not has_event_name and not has_type:
        errors.append(
            {
                "field": _join(prefix, "event"),
                "message": "Event must have either 'event' (name) or 'type' field",
            }
        )

    has_user = bool(event.get("user_id")) or bool(event.get("userId"))
    has_anon = bool(event.get("anonymous_id")) or bool(event.get("anonymousId"))
    if not has_user and not has_anon:
        errors.append(
            {
                "field": _join(prefix, "user_id"),
                "message": "Event must have either 'user_id'/'userId' or 'anonymous_id'/'anonymousId'",
            }
        )

    try:
        Event.model_validate(event)
    except ValidationError as exc:
        errors.extend(_format_errors(exc, prefix=prefix))

    errors.extend(_validate_reserved_event(event, prefix))

    return errors


def _validate_reserved_event(event: dict, prefix: str) -> list[dict]:
    event_name = event.get("event")
    if event_name == FEATURE_FLAG_EXPOSURE_EVENT:
        return _validate_feature_flag_exposure_event(event, prefix)
    if event_name == FRONTEND_ERROR_EVENT:
        return _validate_frontend_error_event(event, prefix)
    if event_name == WEB_VITAL_EVENT:
        return _validate_web_vital_event(event, prefix)

    return []


def _validate_feature_flag_exposure_event(event: dict, prefix: str) -> list[dict]:
    errors: list[dict] = []
    if event.get("type") != "track":
        errors.append({
            "field": _join(prefix, "type"),
            "message": "Reserved event '$feature_flag_exposure' must use type 'track'",
        })

    _validate_feature_flag_exposure_envelope(event, prefix, errors)

    if "properties" not in event:
        errors.append({
            "field": _join(prefix, "properties"),
            "message": "Missing required reserved event properties",
        })
        return errors

    properties = event.get("properties")
    if not isinstance(properties, dict):
        errors.append({
            "field": _join(prefix, "properties"),
            "message": "Reserved event properties must be an object",
        })
        return errors

    keys = set(properties)
    for key in sorted(FEATURE_FLAG_EXPOSURE_KEYS - keys):
        errors.append({
            "field": _join(prefix, f"properties.{key}"),
            "message": "Missing required feature flag exposure property",
        })

    for key in sorted(keys - FEATURE_FLAG_EXPOSURE_KEYS):
        errors.append({
            "field": _join(prefix, f"properties.{key}"),
            "message": "Unknown feature flag exposure property",
        })

    _validate_feature_flag_exposure_types(properties, prefix, errors)
    return errors


def _validate_frontend_error_event(event: dict, prefix: str) -> list[dict]:
    errors: list[dict] = []
    if event.get("type") != "track":
        errors.append({
            "field": _join(prefix, "type"),
            "message": "Reserved event '$frontend_error' must use type 'track'",
        })

    _validate_feature_flag_exposure_envelope(event, prefix, errors)
    properties = _reserved_properties(event, prefix, errors)
    if properties is None:
        return errors

    keys = set(properties)
    for key in sorted(FRONTEND_ERROR_KEYS - keys):
        errors.append({
            "field": _join(prefix, f"properties.{key}"),
            "message": "Missing required frontend error property",
        })

    for key in sorted(keys - FRONTEND_ERROR_KEYS):
        errors.append({
            "field": _join(prefix, f"properties.{key}"),
            "message": "Unknown frontend error property",
        })

    if (
        "error_type" in properties
        and properties["error_type"] not in FRONTEND_ERROR_TYPES
    ):
        errors.append({
            "field": _join(prefix, "properties.error_type"),
            "message": "Property 'error_type' is not a canonical frontend error type",
        })

    for key in ("message", "page", "component", "slot_id", "source", "stack"):
        _validate_string_property(properties, key, prefix, errors)

    _validate_nullable_number_property(properties, "line", prefix, errors)
    _validate_nullable_number_property(properties, "column", prefix, errors)
    _validate_active_flag_properties(properties, prefix, errors)
    return errors


def _validate_web_vital_event(event: dict, prefix: str) -> list[dict]:
    errors: list[dict] = []
    if event.get("type") != "track":
        errors.append({
            "field": _join(prefix, "type"),
            "message": "Reserved event '$web_vital' must use type 'track'",
        })

    _validate_feature_flag_exposure_envelope(event, prefix, errors)
    properties = _reserved_properties(event, prefix, errors)
    if properties is None:
        return errors

    keys = set(properties)
    for key in sorted(WEB_VITAL_KEYS - keys):
        errors.append({
            "field": _join(prefix, f"properties.{key}"),
            "message": "Missing required web vital property",
        })

    for key in sorted(keys - WEB_VITAL_KEYS):
        errors.append({
            "field": _join(prefix, f"properties.{key}"),
            "message": "Unknown web vital property",
        })

    if "metric" in properties and properties["metric"] not in WEB_VITAL_METRICS:
        errors.append({
            "field": _join(prefix, "properties.metric"),
            "message": "Property 'metric' is not a canonical web vital metric",
        })

    if "rating" in properties and properties["rating"] not in WEB_VITAL_RATINGS:
        errors.append({
            "field": _join(prefix, "properties.rating"),
            "message": "Property 'rating' is not a canonical web vital rating",
        })

    _validate_number_property(properties, "value", prefix, errors)
    _validate_number_property(properties, "delta", prefix, errors)
    _validate_string_property(properties, "id", prefix, errors)
    _validate_string_property(properties, "navigation_type", prefix, errors)
    _validate_string_property(properties, "page", prefix, errors)
    _validate_active_flag_properties(properties, prefix, errors)
    return errors


def _reserved_properties(
    event: dict,
    prefix: str,
    errors: list[dict],
) -> dict | None:
    if "properties" not in event:
        errors.append({
            "field": _join(prefix, "properties"),
            "message": "Missing required reserved event properties",
        })
        return None

    properties = event.get("properties")
    if not isinstance(properties, dict):
        errors.append({
            "field": _join(prefix, "properties"),
            "message": "Reserved event properties must be an object",
        })
        return None

    return properties


def _validate_feature_flag_exposure_envelope(
    event: dict,
    prefix: str,
    errors: list[dict],
) -> None:
    keys = set(event)

    for key in sorted(FEATURE_FLAG_EXPOSURE_REQUIRED_ENVELOPE_KEYS - keys):
        errors.append({
            "field": _join(prefix, key),
            "message": "Missing required feature flag exposure envelope field",
        })

    for key in sorted(keys - FEATURE_FLAG_EXPOSURE_ENVELOPE_KEYS):
        errors.append({
            "field": _join(prefix, key),
            "message": "Unknown feature flag exposure envelope field",
        })

    if not event.get("user_id") and not event.get("anonymous_id"):
        errors.append({
            "field": _join(prefix, "user_id"),
            "message": "Feature flag exposure requires canonical user_id or anonymous_id",
        })

    for key in ("user_id", "anonymous_id", "group_id", "timestamp", "message_id", "session_id"):
        if key in event and not _is_non_empty_string(event[key]):
            errors.append({
                "field": _join(prefix, key),
                "message": f"Envelope field '{key}' must be a non-empty string",
            })

    if "context" in event and not isinstance(event["context"], dict):
        errors.append({
            "field": _join(prefix, "context"),
            "message": "Envelope field 'context' must be an object",
        })


def _validate_feature_flag_exposure_types(
    properties: dict,
    prefix: str,
    errors: list[dict],
) -> None:
    if "flag_key" in properties and not _is_non_empty_string(properties["flag_key"]):
        errors.append({
            "field": _join(prefix, "properties.flag_key"),
            "message": "Property 'flag_key' must be a non-empty string",
        })

    if "value" in properties and not isinstance(properties["value"], bool):
        errors.append({
            "field": _join(prefix, "properties.value"),
            "message": "Property 'value' must be a boolean",
        })

    if (
        "reason" in properties
        and properties["reason"] not in FEATURE_FLAG_EXPOSURE_REASONS
    ):
        errors.append({
            "field": _join(prefix, "properties.reason"),
            "message": "Property 'reason' is not a canonical gate evaluation reason",
        })

    _validate_string_property(properties, "rule_id", prefix, errors)
    _validate_nullable_number_property(properties, "bucket", prefix, errors)
    _validate_nullable_number_property(
        properties,
        "rollout_percentage",
        prefix,
        errors,
        minimum=0.0,
        maximum=100.0,
    )
    _validate_string_property(properties, "bucket_by", prefix, errors)

    if "config_version" in properties and not _is_non_negative_int(
        properties["config_version"]
    ):
        errors.append({
            "field": _join(prefix, "properties.config_version"),
            "message": "Property 'config_version' must be a non-negative integer",
        })

    if "source" in properties and properties["source"] not in FEATURE_FLAG_EXPOSURE_SOURCES:
        errors.append({
            "field": _join(prefix, "properties.source"),
            "message": "Property 'source' is not a canonical gate config source",
        })

    _validate_string_property(properties, "page", prefix, errors)


def _validate_string_property(
    properties: dict,
    key: str,
    prefix: str,
    errors: list[dict],
) -> None:
    if key in properties and not isinstance(properties[key], str):
        errors.append({
            "field": _join(prefix, f"properties.{key}"),
            "message": f"Property '{key}' must be a string",
        })


def _validate_nullable_number_property(
    properties: dict,
    key: str,
    prefix: str,
    errors: list[dict],
    minimum: float | None = None,
    maximum: float | None = None,
) -> None:
    if key not in properties:
        return

    value = properties[key]
    if value is None:
        return

    if not _is_finite_number(value):
        errors.append({
            "field": _join(prefix, f"properties.{key}"),
            "message": f"Property '{key}' must be a finite number or null",
        })
        return

    if minimum is not None and value < minimum:
        errors.append({
            "field": _join(prefix, f"properties.{key}"),
            "message": f"Property '{key}' must be greater than or equal to {minimum:g}",
        })
    if maximum is not None and value > maximum:
        errors.append({
            "field": _join(prefix, f"properties.{key}"),
            "message": f"Property '{key}' must be less than or equal to {maximum:g}",
        })


def _validate_number_property(
    properties: dict,
    key: str,
    prefix: str,
    errors: list[dict],
) -> None:
    if key not in properties:
        return

    if not _is_finite_number(properties[key]):
        errors.append({
            "field": _join(prefix, f"properties.{key}"),
            "message": f"Property '{key}' must be a finite number",
        })


def _validate_active_flag_properties(
    properties: dict,
    prefix: str,
    errors: list[dict],
) -> None:
    active_flags = properties.get("active_flags")
    active_versions = properties.get("active_flag_versions")

    if "active_flags" in properties and not isinstance(active_flags, dict):
        errors.append({
            "field": _join(prefix, "properties.active_flags"),
            "message": "Property 'active_flags' must be an object",
        })
        return

    if "active_flag_versions" in properties and not isinstance(active_versions, dict):
        errors.append({
            "field": _join(prefix, "properties.active_flag_versions"),
            "message": "Property 'active_flag_versions' must be an object",
        })
        return

    if not isinstance(active_flags, dict) or not isinstance(active_versions, dict):
        return

    flag_keys = set(active_flags)
    version_keys = set(active_versions)
    if flag_keys != version_keys:
        errors.append({
            "field": _join(prefix, "properties.active_flag_versions"),
            "message": "Property 'active_flag_versions' keys must match active_flags",
        })

    for key, value in active_flags.items():
        if not isinstance(key, str) or not key:
            errors.append({
                "field": _join(prefix, "properties.active_flags"),
                "message": "Property 'active_flags' keys must be non-empty strings",
            })
        if not isinstance(value, bool):
            errors.append({
                "field": _join(prefix, f"properties.active_flags.{key}"),
                "message": "Active flag values must be booleans",
            })

    for key, value in active_versions.items():
        if not isinstance(key, str) or not key:
            errors.append({
                "field": _join(prefix, "properties.active_flag_versions"),
                "message": "Property 'active_flag_versions' keys must be non-empty strings",
            })
        if not _is_non_negative_int(value):
            errors.append({
                "field": _join(prefix, f"properties.active_flag_versions.{key}"),
                "message": "Active flag versions must be non-negative integers",
            })


def _is_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and len(value) > 0


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _format_errors(exc: ValidationError, prefix: str = "") -> list[dict]:
    errors = []
    for err in exc.errors():
        field = _loc_to_field(err["loc"])
        if prefix and field:
            field = f"{prefix}.{field}"
        elif prefix:
            field = prefix
        msg = err["msg"]
        if msg.startswith("Value error, "):
            msg = msg[13:]
        errors.append({"field": field, "message": msg})
    return errors


def _loc_to_field(loc: tuple) -> str:
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            if parts:
                parts[-1] += f"[{item}]"
            else:
                parts.append(f"[{item}]")
        else:
            parts.append(str(item))
    return ".".join(parts)


def _join(prefix: str, field: str) -> str:
    if not prefix:
        return field
    return f"{prefix}.{field}"


def _error(field: str, message: str) -> dict:
    return {"valid": False, "errors": [{"field": field, "message": message}]}
