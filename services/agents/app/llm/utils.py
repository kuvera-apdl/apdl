"""Shared utilities for LLM response handling."""

from __future__ import annotations

import json
from typing import Any


def parse_llm_json(response: str, fallback: Any = None) -> Any:
    """Parse JSON from an LLM response, stripping markdown code fences if present."""
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        if "```json" in response:
            json_str = response.split("```json")[1].split("```")[0].strip()
            return json.loads(json_str)
        return fallback
