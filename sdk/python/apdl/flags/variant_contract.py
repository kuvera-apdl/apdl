"""Strict bounds shared by every APDL weighted-variant runtime."""

from __future__ import annotations

from collections.abc import Sequence


MAX_VARIANTS = 10
MAX_VARIANT_WEIGHT = 9_007_199_254_740_991
MAX_TOTAL_VARIANT_WEIGHT = 9_007_199_254_740_991


def validate_variant_weight_contract(weights: Sequence[object]) -> None:
    """Reject weights that JavaScript cannot represent or sum exactly."""
    if not weights:
        raise ValueError("variants must contain at least one variant")
    if len(weights) > MAX_VARIANTS:
        raise ValueError(f"variants must contain at most {MAX_VARIANTS} entries")

    total_weight = 0
    for weight in weights:
        if (
            type(weight) is not int
            or weight < 0
            or weight > MAX_VARIANT_WEIGHT
        ):
            raise ValueError(
                "variant weight must be a nonnegative JavaScript-safe integer"
            )
        total_weight += weight
        if total_weight > MAX_TOTAL_VARIANT_WEIGHT:
            raise ValueError(
                "total variant weight exceeds the JavaScript-safe integer limit"
            )

    if total_weight <= 0:
        raise ValueError(
            "variant weights must contain at least one positive weight"
        )
