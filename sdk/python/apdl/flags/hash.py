"""Deterministic FNV-1a bucketing.

This is a byte-for-byte port of ``services/config/app/flags/evaluator.py`` and
``sdk/javascript/src/flags/hash.ts``. The three implementations MUST stay in
lockstep so a user buckets identically regardless of where a gate is evaluated.
"""

from __future__ import annotations

_UINT32_MAX = 0xFFFFFFFF
_FNV_OFFSET_BASIS = 2166136261
_FNV_PRIME = 16777619


def hash_bucket(flag_key: str, salt: str, unit_id: str) -> int:
    """FNV-1a 32-bit hash of ``"{flag_key}:{salt}:{unit_id}"``."""
    h = _FNV_OFFSET_BASIS
    for byte in f"{flag_key}:{salt}:{unit_id}".encode("utf-8"):
        h ^= byte
        h = (h * _FNV_PRIME) & _UINT32_MAX
    return h


def percentage_bucket(flag_key: str, salt: str, unit_id: str) -> float:
    """Maps the hash into a stable ``[0, 100)`` bucket."""
    return (hash_bucket(flag_key, salt, unit_id) / _UINT32_MAX) * 100.0


def is_in_rollout(flag_key: str, salt: str, unit_id: str, percentage: float) -> bool:
    """Whether an evaluation unit falls within a rollout ``percentage``."""
    if percentage >= 100.0:
        return True
    if percentage <= 0.0:
        return False
    return percentage_bucket(flag_key, salt, unit_id) < percentage
