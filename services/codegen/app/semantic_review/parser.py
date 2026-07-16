"""Exact JSON parsing for independent semantic-review model output."""

from __future__ import annotations

import json

from pydantic import ValidationError

from app.semantic_review.models import (
    ModelReviewResponse,
    ReviewParseError,
    ReviewVerdict,
)
from app.semantic_review.references import (
    ReviewReferenceIndex,
    validate_model_response_references,
    validate_verdict_references,
)


def parse_model_review_response(
    text: str, *, reference_index: ReviewReferenceIndex
) -> ModelReviewResponse:
    """Parse only one complete JSON object; no fence/brace extraction fallback."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewParseError(f"Review response is not exact JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ReviewParseError("Review response must be one JSON object.")
    try:
        # Pydantic's strict JSON path accepts JSON enum strings while still
        # rejecting Python-side coercion; validating the decoded dict would
        # incorrectly require already-instantiated Enum objects.
        response = ModelReviewResponse.model_validate_json(text)
    except ValidationError as exc:
        raise ReviewParseError(f"Review response violates the strict schema: {exc}") from exc
    validate_model_response_references(response, reference_index)
    return response


def parse_review_verdict(
    text: str, *, reference_index: ReviewReferenceIndex
) -> ReviewVerdict:
    """Parse a persisted final ``review_verdict@1`` without permissive fallbacks."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewParseError(f"Review verdict is not exact JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ReviewParseError("Review verdict must be one JSON object.")
    try:
        verdict = ReviewVerdict.model_validate_json(text)
    except ValidationError as exc:
        raise ReviewParseError(f"Review verdict violates the strict schema: {exc}") from exc
    validate_verdict_references(verdict, reference_index)
    return verdict
