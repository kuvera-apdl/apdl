"""Shared utilities for the config service."""

import json
import re

from fastapi import Request

_KEY_PATTERN = re.compile(r"^proj_([a-zA-Z0-9]{1,64})_([a-zA-Z0-9]{16,})$")


def serialize_flag(f: dict, include_description: bool = True) -> dict:
    """Convert a flag DB row to the API representation."""
    rules_json = f.get("rules_json", "[]")
    variants_json = f.get("variants_json", "[]")
    entry: dict = {
        "key": f["key"],
        "enabled": f["enabled"],
        "variant_type": f.get("variant_type", "boolean"),
        "default_value": f.get("default_value", "false"),
        "rollout_percentage": f.get("rollout_percentage", 100.0),
        "rules": json.loads(rules_json) if rules_json else [],
        "variants": json.loads(variants_json) if variants_json else [],
    }
    if include_description:
        entry["description"] = f.get("description", "")
    entry["updated_at"] = f.get("updated_at", "")
    return entry


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
