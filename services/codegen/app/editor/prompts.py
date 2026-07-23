"""Bounded canonical storage for Codegen model-prompt transcripts."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.editor.environment import MODEL_PROVIDER_ENV, MODEL_PROVIDER_ROUTING_ENV
from app.safety.secrets import redact_secrets


PROMPT_ENTRY_MAX_BYTES = 32 * 1024
PROMPT_TRANSCRIPT_MAX_BYTES = 128 * 1024
_TEXT_FIELDS = ("user", "system", "notes", "label", "stage")
_MODEL_PROVIDER_SECRET_ENV = tuple(
    name for name in MODEL_PROVIDER_ENV if name not in MODEL_PROVIDER_ROUTING_ENV
)
_TRANSCRIPT_MARKER_RE = re.compile(
    r"^\[…transcript truncated: omitted (?P<entries>[0-9]+) middle prompt "
    r"entries \((?P<bytes>[0-9]+) serialized bytes\)…\]$"
)


def serialized_prompt_bytes(value: Any) -> int:
    """Return the exact UTF-8 size used by the JSONB write path."""
    return len(json.dumps(value).encode("utf-8"))


def _optional_text(value: Any) -> str | None:
    return None if value is None else _redact_text(str(value))


def _redact_text(value: str) -> str:
    """Remove configured provider credentials and common secret-shaped values."""
    configured = {
        secret for name in _MODEL_PROVIDER_SECRET_ENV if (secret := os.getenv(name))
    }
    return redact_secrets(value, protected_values=configured)[0]


def _canonical_prompt(prompt: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the one strict prompt-entry shape accepted by the operator UI."""
    return {
        "stage": _redact_text(str(prompt.get("stage") or "unknown")),
        "label": _redact_text(str(prompt.get("label") or "Prompt")),
        "system": _optional_text(prompt.get("system")),
        "user": _redact_text(str(prompt.get("user") or "")),
        "notes": _optional_text(prompt.get("notes")),
    }


def _middle_excerpt(text: str, kept_characters: int) -> str:
    """Preserve prompt context and latest feedback around an explicit marker."""
    kept_characters = max(0, min(kept_characters, len(text)))
    if kept_characters == len(text):
        return text
    omitted = len(text) - kept_characters
    marker = f"\n[…truncated {omitted} characters…]\n"
    head = (kept_characters + 1) // 2
    tail = kept_characters // 2
    return text[:head] + marker + (text[-tail:] if tail else "")


def _fit_field(
    entry: dict[str, Any], key: str, original: str
) -> tuple[dict[str, Any], bool]:
    """Maximize one retained field while fitting the exact per-entry byte cap."""
    low = 0
    high = max(0, len(original) - 1)
    best: str | None = None
    while low <= high:
        kept = (low + high) // 2
        candidate = _middle_excerpt(original, kept)
        trial = {**entry, key: candidate}
        if serialized_prompt_bytes(trial) <= PROMPT_ENTRY_MAX_BYTES:
            best = candidate
            low = kept + 1
        else:
            high = kept - 1
    if best is not None:
        return {**entry, key: best}, True
    return {**entry, key: _middle_excerpt(original, 0)}, False


def bound_prompt_entry(prompt: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize one entry and enforce its serialized-byte ceiling."""
    bounded = _canonical_prompt(prompt)
    if serialized_prompt_bytes(bounded) <= PROMPT_ENTRY_MAX_BYTES:
        return bounded

    remaining = {
        key
        for key in _TEXT_FIELDS
        if isinstance(bounded.get(key), str) and bounded[key]
    }
    while serialized_prompt_bytes(bounded) > PROMPT_ENTRY_MAX_BYTES and remaining:
        key = max(
            remaining,
            key=lambda field: len(str(bounded[field]).encode("utf-8")),
        )
        original = str(bounded[key])
        bounded, fits = _fit_field(bounded, key, original)
        remaining.remove(key)
        if fits:
            break

    if serialized_prompt_bytes(bounded) > PROMPT_ENTRY_MAX_BYTES:
        raise ValueError("canonical prompt metadata exceeds the per-entry limit")
    return bounded


def _aggregate_marker(
    *, omitted_entries: int, omitted_serialized_bytes: int
) -> dict[str, Any]:
    return {
        "stage": "transcript",
        "label": "Prompt transcript truncated",
        "system": None,
        "user": "",
        "notes": (
            "[…transcript truncated: omitted "
            f"{omitted_entries} middle prompt entries "
            f"({omitted_serialized_bytes} serialized bytes)…]"
        ),
    }


def _aggregate_marker_metadata(entry: Mapping[str, Any]) -> tuple[int, int] | None:
    """Read only markers emitted by this module, never user-authored lookalikes."""
    if (
        entry.get("stage") != "transcript"
        or entry.get("label") != "Prompt transcript truncated"
        or entry.get("system") is not None
        or entry.get("user") != ""
        or not isinstance(entry.get("notes"), str)
    ):
        return None
    match = _TRANSCRIPT_MARKER_RE.fullmatch(entry["notes"])
    if match is None:
        return None
    return int(match["entries"]), int(match["bytes"])


def bound_prompt_transcript(
    prompts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Bound a transcript, preserving first/newest entries and omission totals."""
    inherited_omitted_entries = 0
    inherited_omitted_bytes = 0
    bounded: list[dict[str, Any]] = []
    for prompt in prompts:
        entry = bound_prompt_entry(prompt)
        marker = _aggregate_marker_metadata(entry)
        if marker is None:
            bounded.append(entry)
        else:
            inherited_omitted_entries += marker[0]
            inherited_omitted_bytes += marker[1]
    if not bounded:
        if inherited_omitted_entries:
            return [
                _aggregate_marker(
                    omitted_entries=inherited_omitted_entries,
                    omitted_serialized_bytes=inherited_omitted_bytes,
                )
            ]
        return []

    entry_sizes = [serialized_prompt_bytes(entry) for entry in bounded]
    first = bounded[0]
    suffix: list[dict[str, Any]] = []

    def candidate(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        omitted_end = len(bounded) - len(values)
        omitted_count = inherited_omitted_entries + max(0, omitted_end - 1)
        if omitted_count == 0:
            return [first, *values]
        omitted_bytes = inherited_omitted_bytes + sum(entry_sizes[1:omitted_end])
        return [
            first,
            _aggregate_marker(
                omitted_entries=omitted_count,
                omitted_serialized_bytes=omitted_bytes,
            ),
            *values,
        ]

    complete = candidate(bounded[1:])
    if serialized_prompt_bytes(complete) <= PROMPT_TRANSCRIPT_MAX_BYTES:
        return complete

    for entry in reversed(bounded[1:]):
        proposed_suffix = [entry, *suffix]
        if (
            serialized_prompt_bytes(candidate(proposed_suffix))
            > PROMPT_TRANSCRIPT_MAX_BYTES
        ):
            break
        suffix = proposed_suffix

    result = candidate(suffix)
    if serialized_prompt_bytes(result) > PROMPT_TRANSCRIPT_MAX_BYTES:
        raise ValueError("prompt transcript metadata exceeds the aggregate limit")
    return result


def append_prompt(transcript: list[dict[str, Any]], prompt: Mapping[str, Any]) -> None:
    """Append while enforcing both entry and running aggregate bounds."""
    transcript[:] = bound_prompt_transcript([*transcript, prompt])


def replace_latest_prompt(
    transcript: list[dict[str, Any]], prompt: Mapping[str, Any]
) -> None:
    """Replace the newest entry without bypassing either storage bound."""
    if not transcript:
        raise ValueError("cannot replace a prompt in an empty transcript")
    transcript[:] = bound_prompt_transcript([*transcript[:-1], prompt])
