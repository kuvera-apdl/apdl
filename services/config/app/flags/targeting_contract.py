"""Strict, cross-runtime targeting value contract.

These helpers deliberately implement JSON semantics instead of Python coercion.
The JavaScript SDK, Python SDK, Admin evaluator, and Config evaluator must stay
in lockstep; ``fixtures/gates/targeting.json`` is the executable contract.
"""

from __future__ import annotations

import math
import re
from typing import Any

MAX_RULES = 50
MAX_CONDITIONS_PER_RULE = 20
MAX_IDENTIFIER_LENGTH = 128
MAX_STRING_LENGTH = 256
MAX_MEMBERSHIP_VALUES = 100

NUMERIC_PATTERN = (
    r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$"
)
_NUMERIC_RE = re.compile(NUMERIC_PATTERN, re.ASCII)

EQUALITY_OPERATORS = {"equals", "not_equals"}
STRING_OPERATORS = {
    "contains",
    "not_contains",
    "starts_with",
    "ends_with",
}
NUMERIC_OPERATORS = {"gt", "gte", "lt", "lte"}
MEMBERSHIP_OPERATORS = {"in", "not_in"}
PRESENCE_OPERATORS = {"exists", "not_exists"}
SUPPORTED_OPERATORS = (
    EQUALITY_OPERATORS
    | STRING_OPERATORS
    | NUMERIC_OPERATORS
    | MEMBERSHIP_OPERATORS
    | PRESENCE_OPERATORS
)


def is_identifier(value: Any) -> bool:
    """Whether ``value`` is a bounded, non-empty targeting identifier."""
    return isinstance(value, str) and 0 < len(value) <= MAX_IDENTIFIER_LENGTH


def is_bounded_string(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= MAX_STRING_LENGTH


def is_json_number(value: Any) -> bool:
    """JSON number accepted by every runtime (booleans are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def is_scalar(value: Any) -> bool:
    """Canonical comparable scalar: bounded string, bool, or finite number."""
    return is_bounded_string(value) or isinstance(value, bool) or is_json_number(value)


def scalar_equal(left: Any, right: Any) -> bool:
    """Type-strict JSON scalar equality, with bool distinct from number."""
    if not is_scalar(left) or not is_scalar(right):
        return False
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    return float(left) == float(right)


def parse_numeric(value: Any) -> float | None:
    """Parse only finite JSON numbers or the canonical ASCII decimal grammar."""
    if is_json_number(value):
        return float(value)
    if not is_bounded_string(value) or _NUMERIC_RE.fullmatch(value) is None:
        return None
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def is_membership_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and 0 < len(value) <= MAX_MEMBERSHIP_VALUES
        and all(is_scalar(item) for item in value)
    )


def is_condition_value_valid(operator: str, value: Any) -> bool:
    """Validate one value-taking condition without permissive fallbacks."""
    if operator in EQUALITY_OPERATORS:
        return is_scalar(value)
    if operator in STRING_OPERATORS:
        return is_bounded_string(value)
    if operator in NUMERIC_OPERATORS:
        return parse_numeric(value) is not None
    if operator in MEMBERSHIP_OPERATORS:
        return is_membership_list(value)
    return False
