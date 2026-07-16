"""Deterministic, bounded rendering for editor and reviewer context."""

from __future__ import annotations

import json
from typing import Any

from app.inspection.models import DependencySlice, InspectionSnapshot

_DEFAULT_RENDER_CAP = 60_000


def _cap_excerpts(value: Any, excerpt_chars: int) -> Any:
    if isinstance(value, list):
        return [_cap_excerpts(item, excerpt_chars) for item in value]
    if isinstance(value, dict):
        rendered = {
            key: _cap_excerpts(item, excerpt_chars) for key, item in value.items()
        }
        excerpt = rendered.get("excerpt")
        if isinstance(excerpt, str) and len(excerpt) > excerpt_chars:
            rendered["excerpt"] = excerpt[:excerpt_chars] + "\n[…excerpt truncated…]"
        return rendered
    return value


def _render(title: str, payload: dict[str, Any], max_chars: int) -> str:
    if max_chars <= 200:
        raise ValueError("render budget must exceed 200 characters")
    body = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    prefix = f"## {title}\n\n```json\n"
    suffix = "\n```"
    rendered = prefix + body + suffix
    if len(rendered) <= max_chars:
        return rendered
    available = max_chars - len(prefix) - len(suffix) - 32
    clipped = body[: max(0, available)]
    boundary = clipped.rfind("\n")
    if boundary > 0:
        clipped = clipped[:boundary]
    return prefix + clipped + "\n[…render truncated…]" + suffix


def render_inspection_snapshot(
    snapshot: InspectionSnapshot,
    *,
    max_chars: int = _DEFAULT_RENDER_CAP,
    excerpt_chars: int = 1200,
) -> str:
    payload = _cap_excerpts(snapshot.model_dump(mode="json"), excerpt_chars)
    return _render("Repository inspection snapshot", payload, max_chars)


def render_dependency_slice(
    dependency_slice: DependencySlice,
    *,
    max_chars: int = _DEFAULT_RENDER_CAP,
    excerpt_chars: int = 1200,
) -> str:
    payload = _cap_excerpts(dependency_slice.model_dump(mode="json"), excerpt_chars)
    return _render("Changed-file dependency slice", payload, max_chars)
