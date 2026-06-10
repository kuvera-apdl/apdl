"""Parsing of raw config-service payloads into validated flag configs.

Port of ``parseFlagConfigResult`` from ``sdk/javascript/src/flags/schema.ts``.
A payload must be the canonical collection envelope
``{schema_version: 2, project_id: <non-empty>, flags: [...]}``; bare lists and
the legacy ``schema_version: 1`` envelope are rejected. Individual malformed
flags that still carry a ``key`` are surfaced as ``invalid_keys`` rather than
failing the whole batch; a structurally unrecognizable payload yields ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .models import GateConfig

_COLLECTION_KEYS = {"schema_version", "project_id", "flags"}


@dataclass
class FlagConfigParseResult:
    flags: list[GateConfig] = field(default_factory=list)
    invalid_keys: list[str] = field(default_factory=list)


def _extract_candidates(data: Any) -> list[Any] | None:
    if not isinstance(data, dict) or not set(data.keys()) <= _COLLECTION_KEYS:
        return None
    if data.get("schema_version") != 2:
        return None
    project_id = data.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        return None
    flags = data.get("flags")
    if not isinstance(flags, list):
        return None
    return flags


def parse_flag_config_result(data: Any) -> FlagConfigParseResult | None:
    """Validates a raw payload, splitting valid gates from invalid keys.

    Returns ``None`` when the payload's overall shape is unrecognizable.
    """
    candidates = _extract_candidates(data)
    if candidates is None:
        return None

    result = FlagConfigParseResult()
    for candidate in candidates:
        try:
            result.flags.append(GateConfig.model_validate(candidate))
            continue
        except ValidationError:
            pass

        # A malformed-but-keyed record degrades to an invalid key; anything
        # else means the payload itself can't be trusted.
        if isinstance(candidate, dict):
            key = candidate.get("key")
            if isinstance(key, str) and key:
                result.invalid_keys.append(key)
                continue
        return None

    return result


def parse_flag_configs(data: Any) -> list[GateConfig] | None:
    """Strict variant: returns ``None`` if any record is invalid."""
    result = parse_flag_config_result(data)
    if result is None or result.invalid_keys:
        return None
    return result.flags
