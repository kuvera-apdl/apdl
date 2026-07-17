"""Strict response-contract tests for typed property breakdowns."""

import pytest
from pydantic import ValidationError

from app.models.schemas import BreakdownResult


VALID_ROW = {
    "selector": "purchase",
    "property_type": "integer",
    "property_value": "1",
    "event_count": 7,
    "unique_users": 5,
}


def test_breakdown_result_accepts_only_the_canonical_typed_shape():
    result = BreakdownResult.model_validate(VALID_ROW)

    assert result.model_dump() == VALID_ROW


@pytest.mark.parametrize(
    "change",
    [
        {"property_type": "number"},
        {"property_value": 1},
        {"event_count": -1},
        {"event_count": "7"},
        {"unexpected": "field"},
    ],
)
def test_breakdown_result_rejects_ambiguous_or_invalid_rows(change):
    row = {**VALID_ROW, **change}

    with pytest.raises(ValidationError):
        BreakdownResult.model_validate(row)


def test_breakdown_result_requires_the_type_discriminator():
    row = dict(VALID_ROW)
    del row["property_type"]

    with pytest.raises(ValidationError):
        BreakdownResult.model_validate(row)
