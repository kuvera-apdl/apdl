"""Shared utilities for the config service."""

from datetime import date, datetime

from app.models.schemas import ClientFlagConfig, FlagConfig

SCHEMA_VERSION = 2


def _json_safe(value):
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def serialize_flag(f: dict) -> dict:
    """Convert a canonical flag DB row to the full admin API representation."""
    payload = {
        "key": f["key"],
        "project_id": f.get("project_id", ""),
        "name": f.get("name", ""),
        "state": f.get("state", "draft"),
        "owners": f.get("owners", []),
        "review_by": _json_safe(f.get("review_by")),
        "description": f.get("description", ""),
        "enabled": f["enabled"],
        "default_variant": f["default_variant"],
        "variants": f["variants"],
        "rules": f.get("rules", []),
        "fallthrough": f["fallthrough"],
        "salt": f.get("salt", ""),
        "evaluation_mode": f.get("evaluation_mode", "client"),
        "auto_disable": f.get("auto_disable", True),
        "guardrails": f.get("guardrails", []),
        "disabled_reason": f.get("disabled_reason", ""),
        "disabled_by": f.get("disabled_by", ""),
        "disabled_at": _json_safe(f.get("disabled_at")),
        "version": f.get("version", 1),
        "created_at": _json_safe(f.get("created_at", "")),
        "updated_at": _json_safe(f.get("updated_at", "")),
        "archived_at": _json_safe(f.get("archived_at")),
    }
    return FlagConfig.model_validate(payload).model_dump(mode="json")


def serialize_client_flag(f: dict) -> dict:
    """Convert a canonical flag DB row to the SDK bootstrap representation."""
    payload = {
        "key": f["key"],
        "enabled": f["enabled"],
        "default_variant": f["default_variant"],
        "variants": f["variants"],
        "salt": f.get("salt", ""),
        "rules": f.get("rules", []),
        "fallthrough": f["fallthrough"],
        "version": f.get("version", 1),
    }
    return ClientFlagConfig.model_validate(payload).model_dump(mode="json")


def serialize_flag_collection(project_id: str, flags: list[dict]) -> dict:
    """Return the canonical SDK flag collection envelope."""
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "flags": [serialize_client_flag(flag) for flag in flags],
    }
