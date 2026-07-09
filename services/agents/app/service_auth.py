"""Project-scoped credentials for internal APDL service calls."""

from __future__ import annotations

import json
import os
import re

_API_KEY_PATTERN = re.compile(
    r"^proj_(?P<project_id>[A-Za-z0-9]{1,64})_[A-Za-z0-9]{16,128}$"
)


def service_headers(project_id: str) -> dict[str, str]:
    """Return a service credential for exactly one project.

    Production uses APDL_SERVICE_API_KEYS. Local development may reuse the
    single credential provisioned from APDL_DEV_API_KEY when its project hint
    matches the requested project.
    """
    raw = os.environ.get("APDL_SERVICE_API_KEYS", "")
    if raw:
        try:
            keys = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("APDL_SERVICE_API_KEYS must be a JSON object") from exc
        if not isinstance(keys, dict):
            raise RuntimeError("APDL_SERVICE_API_KEYS must be a JSON object")
        api_key = keys.get(project_id)
        if api_key is not None:
            match = (
                _API_KEY_PATTERN.fullmatch(api_key)
                if isinstance(api_key, str)
                else None
            )
            if match is None or match.group("project_id") != project_id:
                raise RuntimeError(
                    f"APDL_SERVICE_API_KEYS[{project_id!r}] must be a valid key "
                    "for that project"
                )
            return {"X-API-Key": api_key}

    dev_key = os.environ.get("APDL_DEV_API_KEY", "")
    match = _API_KEY_PATTERN.fullmatch(dev_key)
    if match is not None and match.group("project_id") == project_id:
        return {"X-API-Key": dev_key}

    raise RuntimeError(f"No service API key configured for project {project_id}")
