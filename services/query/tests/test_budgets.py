"""Hard developer-preview query-budget contract tests."""

import pytest
from pydantic import ValidationError

from app.models.schemas import (
    EventCountRequest,
    EventPropertyFilter,
    GuardrailEvaluateRequest,
)


def _event_count(start_date: str, end_date: str) -> dict:
    return {
        "project_id": "demo",
        "start_date": start_date,
        "end_date": end_date,
        "selectors": [{"event_name": "purchase", "filters": []}],
    }


def test_date_range_accepts_exactly_ninety_days():
    request = EventCountRequest.model_validate(
        _event_count("2026-01-01", "2026-04-01")
    )

    assert (request.end_date - request.start_date).days == 90


def test_date_range_rejects_more_than_ninety_days():
    with pytest.raises(ValidationError, match="must not exceed 90 days"):
        EventCountRequest.model_validate(
            _event_count("2026-01-01", "2026-04-02")
        )


def test_guardrail_window_rejects_more_than_ninety_days():
    with pytest.raises(ValidationError, match="less than or equal to 129600"):
        GuardrailEvaluateRequest.model_validate(
            {
                "project_id": "apiasport",
                "flag_key": "checkout",
                "default_variant": "control",
                "variants": [
                    {"key": "control", "weight": 1},
                    {"key": "treatment", "weight": 1},
                ],
                "guardrail": {
                    "metric": "frontend_error_count",
                    "threshold": "at_least_one",
                    "window_minutes": 129_601,
                },
            }
        )


def test_membership_filter_rejects_more_than_one_hundred_values():
    with pytest.raises(ValidationError, match="accepts at most 100 values"):
        EventPropertyFilter.model_validate(
            {
                "property": "plan",
                "operator": "in",
                "value": [f"plan-{index}" for index in range(101)],
            }
        )


@pytest.mark.parametrize(
    "operator,value",
    [
        ("eq", "x" * 1_025),
        ("contains", "x" * 1_025),
        ("not_in", ["x" * 1_025]),
    ],
)
def test_filter_rejects_overlong_scalar_and_membership_strings(operator, value):
    with pytest.raises(ValidationError, match="1024|bounded finite scalar"):
        EventPropertyFilter.model_validate(
            {"property": "plan", "operator": operator, "value": value}
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 10**400])
def test_filter_rejects_non_finite_or_unrepresentable_numbers(value):
    with pytest.raises(ValidationError, match="finite"):
        EventPropertyFilter.model_validate(
            {"property": "score", "operator": "gte", "value": value}
        )
