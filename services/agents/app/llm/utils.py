"""Shared utilities for LLM response handling."""

from __future__ import annotations

import json
from typing import Any


def parse_llm_json(response: str, fallback: Any = None) -> Any:
    """Parse JSON from an LLM response, tolerating markdown code fences.

    Never raises: if no candidate parses cleanly, ``fallback`` is returned.
    """
    if not isinstance(response, str):
        return fallback

    candidates = [response]
    if "```json" in response:
        candidates.append(response.split("```json", 1)[1].split("```", 1)[0])
    elif "```" in response:
        candidates.append(response.split("```", 1)[1].split("```", 1)[0])

    for candidate in candidates:
        try:
            return json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
    return fallback
