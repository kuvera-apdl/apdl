"""Event batch validation.

Returns the format: ``{"valid": bool, "errors": [{"field": str, "message": str}]}``.

Cross-field checks (event-name-or-type, user/anon identifier, batch size) live
here rather than as model validators so that each failure is attributed to the
specific field a client would expect, and so that multiple errors per event are
collected in a single response instead of short-circuiting on the first.
"""

from pydantic import ValidationError

from app.models.schemas import MAX_BATCH_SIZE, Event


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

    return errors


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
