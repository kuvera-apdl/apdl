"""Shared JSONB coercion for the store layer.

asyncpg hands ``JSONB`` columns back as ``str``; the in-memory test fakes hand
them back as ``dict``. Both store modules need the same str-or-dict → dict
normalization, so it lives here once rather than being copied per module.
"""

from __future__ import annotations

import json
from typing import Any


def loads_jsonb(value: Any) -> dict[str, Any]:
    """Coerce a JSONB column (str from asyncpg, dict from fakes) to a dict."""
    if isinstance(value, str):
        return json.loads(value)
    return value or {}
