"""Event batch validation using Pydantic models.

Validates the same rules as the C++ schema.cpp implementation and returns
the same error format: {"valid": bool, "errors": [{"field": str, "message": str}]}.
"""

from pydantic import ValidationError

from app.models.schemas import EventBatch


def validate_event_batch(body: object) -> dict:
    """Validate a full event batch payload.

    Returns {"valid": bool, "errors": [{"field": str, "message": str}, ...]}.
    """
    if not isinstance(body, dict):
        return {"valid": False, "errors": [{"field": "body", "message": "Request body must be a JSON object"}]}

    if "events" not in body:
        return {"valid": False, "errors": [{"field": "events", "message": "Missing required field 'events'"}]}

    if not isinstance(body["events"], list):
        return {"valid": False, "errors": [{"field": "events", "message": "Field 'events' must be an array"}]}

    try:
        EventBatch.model_validate(body)
        return {"valid": True, "errors": []}
    except ValidationError as exc:
        return {"valid": False, "errors": _format_errors(exc)}


def _format_errors(exc: ValidationError) -> list[dict]:
    errors = []
    for err in exc.errors():
        field = _loc_to_field(err["loc"])
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
