"""Shared utilities for the config service."""

import re
from datetime import date, datetime

from fastapi import Request

_KEY_PATTERN = re.compile(r"^proj_([a-zA-Z0-9]{1,64})_([a-zA-Z0-9]{16,})$")
SCHEMA_VERSION = 1


def _json_safe(value):
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def serialize_flag(f: dict) -> dict:
    """Convert a canonical flag DB row to the full admin API representation."""
    return {
        "key": f["key"],
        "project_id": f.get("project_id", ""),
        "name": f.get("name", ""),
        "state": f.get("state", "draft"),
        "owners": f.get("owners", []),
        "review_by": _json_safe(f.get("review_by")),
        "description": f.get("description", ""),
        "enabled": f["enabled"],
        "default_value": f.get("default_value", False),
        "rules": f.get("rules", []),
        "fallthrough": f.get("fallthrough", {}),
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


def serialize_client_flag(f: dict) -> dict:
    """Convert a canonical flag DB row to the SDK bootstrap representation."""
    return {
        "key": f["key"],
        "enabled": f["enabled"],
        "default_value": f.get("default_value", False),
        "salt": f.get("salt", ""),
        "rules": f.get("rules", []),
        "fallthrough": f.get("fallthrough", {}),
        "version": f.get("version", 1),
    }


def serialize_flag_collection(project_id: str, flags: list[dict]) -> dict:
    """Return the canonical SDK flag collection envelope."""
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "flags": [serialize_client_flag(flag) for flag in flags],
    }


def extract_project_id(request: Request) -> str:
    """Extract the project_id from the API key header, query params, or direct param.

    Checks in order:
    1. X-API-Key header  (format: proj_{project_id}_{secret})
    2. api_key query parameter (same format)
    3. project_id query parameter (raw project ID)
    """
    api_key = request.headers.get("x-api-key") or request.query_params.get(
        "api_key", ""
    )
    m = _KEY_PATTERN.match(api_key)
    if m:
        return m.group(1)
    return request.query_params.get("project_id", "")
