"""Shared utilities for LLM response handling."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)


def _balanced_region(text: str) -> str | None:
    """Extract the first balanced ``{...}`` or ``[...]`` region, if any.

    Handles prose-wrapped JSON ("Here is the JSON: {...} hope that helps").
    Brace counting ignores brackets inside string literals.
    """
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        return None
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_llm_json(response: str, fallback: Any = None) -> Any:
    """Parse JSON from an LLM response, tolerating markdown code fences.

    Tries, in order: the raw response, any fenced block (```json or bare,
    case-insensitive), and the first balanced {...}/[...] region embedded in
    prose. Never raises: if no candidate parses cleanly, ``fallback`` is
    returned.
    """
    if not isinstance(response, str):
        return fallback

    candidates = [response]
    candidates.extend(m.group(1) for m in _FENCE_RE.finditer(response))
    region = _balanced_region(response)
    if region is not None:
        candidates.append(region)

    for candidate in candidates:
        try:
            return json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
    return fallback
