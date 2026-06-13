"""Unit tests for selector SQL generation and analytics query builders."""

import pytest
from pydantic import ValidationError

from app.clickhouse.queries import (
    EXPERIMENT_EXPOSURES_QUERY,
    EXPERIMENT_METRICS_QUERY,
    build_event_count_query,
    build_feature_flag_frontend_error_guardrail_query,
    build_funnel_query,
)
from app.clickhouse.selectors import build_selector_condition, selector_label
from app.models.schemas import EventSelector


def _selector(event_name: str, filters: list[dict] | None = None) -> EventSelector:
    return EventSelector.model_validate(
        {
            "event_name": event_name,
            "filters": filters or [],
        }
    )


class TestSelectorSql:
    def test_eq_filter_uses_parameterized_json_extraction(self):
        selector = _selector(
            "$click",
            [{"property": "href", "operator": "eq", "value": "/catalog"}],
        )
        params = {}

        sql = build_selector_condition(selector, params, "unit")

        assert "event_name = %(unit_event_name)s" in sql
        assert "JSONHas(properties, %(unit_filter_0_property)s)" in sql
        assert "JSONExtractString(properties, %(unit_filter_0_property)s)" in sql
        assert "= %(unit_filter_0_value)s" in sql
        assert "/catalog" not in sql
        assert params["unit_event_name"] == "$click"
        assert params["unit_filter_0_property"] == "href"
        assert params["unit_filter_0_value"] == "/catalog"

    def test_contains_filter_uses_position_function(self):
        selector = _selector(
            "$click",
            [{"property": "text", "operator": "contains", "value": "Start"}],
        )
        params = {}

        sql = build_selector_condition(selector, params, "unit")

        assert "positionCaseSensitive" in sql
        assert params["unit_filter_0_value"] == "Start"

    def test_in_filter_uses_parameterized_value_list(self):
        selector = _selector(
            "signup",
            [{"property": "plan", "operator": "in", "value": ["pro", "team"]}],
        )
        params = {}

        sql = build_selector_condition(selector, params, "unit")

        assert "IN (%(unit_filter_0_value_0)s, %(unit_filter_0_value_1)s)" in sql
        assert params["unit_filter_0_value_0"] == "pro"
        assert params["unit_filter_0_value_1"] == "team"

    def test_numeric_filter_uses_float_extraction(self):
        selector = _selector(
            "purchase",
            [{"property": "revenue", "operator": "gte", "value": 100}],
        )
        params = {}

        sql = build_selector_condition(selector, params, "unit")

        assert "JSONExtractFloat(properties, %(unit_filter_0_property)s)" in sql
        assert ">= %(unit_filter_0_value)s" in sql
        assert params["unit_filter_0_value"] == 100

    def test_exists_filter_does_not_accept_value(self):
        selector = _selector(
            "$pageview",
            [{"property": "path", "operator": "exists"}],
        )
        params = {}

        sql = build_selector_condition(selector, params, "unit")

        assert sql.count("JSONHas(properties, %(unit_filter_0_property)s)") == 1
        assert "unit_filter_0_value" not in params

    def test_selector_label_includes_structured_filters(self):
        selector = _selector(
            "$click",
            [{"property": "href", "operator": "eq", "value": "/pricing"}],
        )

        assert selector_label(selector) == "$click[href eq /pricing]"


class TestSelectorValidation:
    def test_rejects_invalid_operator(self):
        with pytest.raises(ValidationError):
            _selector(
                "$click",
                [{"property": "href", "operator": "starts_with", "value": "/"}],
            )

    def test_rejects_unsafe_property_name(self):
        with pytest.raises(ValidationError):
            _selector(
                "$click",
                [{"property": "href); DROP TABLE events", "operator": "eq", "value": "/"}],
            )

    def test_rejects_unsupported_scalar_value_type(self):
        with pytest.raises(ValidationError):
            _selector(
                "$click",
                [{"property": "href", "operator": "eq", "value": {"url": "/"}}],
            )

    def test_rejects_mixed_list_value_types(self):
        with pytest.raises(ValidationError):
            _selector(
                "$click",
                [{"property": "href", "operator": "in", "value": ["/", True]}],
            )

    def test_rejects_boolean_numeric_comparison(self):
        with pytest.raises(ValidationError):
            _selector(
                "$click",
                [{"property": "is_primary", "operator": "gt", "value": True}],
            )

    def test_rejects_null_value_for_exists_operator(self):
        with pytest.raises(ValidationError):
            _selector(
                "$click",
                [{"property": "href", "operator": "exists", "value": None}],
            )


class TestQueryBuilders:
    def test_count_query_returns_one_row_per_selector(self):
        selectors = [
            _selector("$click", [{"property": "href", "operator": "eq", "value": "/a"}]),
            _selector("$click", [{"property": "href", "operator": "eq", "value": "/b"}]),
        ]
        params = {
            "project_id": "demo",
            "start_date": "2025-01-01",
            "end_date": "2025-01-31",
        }

        sql = build_event_count_query(selectors, params)

        assert "UNION ALL" in sql
        assert "count_0_label" in params
        assert params["count_0_label"] == "$click[href eq /a]"
        assert params["count_1_label"] == "$click[href eq /b]"

    def test_funnel_query_uses_parameterized_step_conditions(self):
        params = {
            "project_id": "demo",
            "start_date": "2025-01-01",
            "end_date": "2025-01-31",
        }
        sql = build_funnel_query(
            [
                _selector("view"),
                _selector(
                    "$click",
                    [{"property": "href", "operator": "eq", "value": "/checkout"}],
                ),
            ],
            params,
            window_seconds=3600,
        )

        assert "windowFunnel(3600000)" in sql
        assert "funnel_step_0_event_name" in params
        assert "funnel_step_1_filter_0_value" in params
        assert "/checkout" not in sql
        assert "OR" in sql

    def test_experiment_queries_use_feature_flag_exposures(self):
        combined_sql = f"{EXPERIMENT_EXPOSURES_QUERY}\n{EXPERIMENT_METRICS_QUERY}"

        assert "FROM feature_flag_exposures" in EXPERIMENT_EXPOSURES_QUERY
        assert "FROM feature_flag_exposures" in EXPERIMENT_METRICS_QUERY
        assert "flag_key = %(flag_key)s" in combined_sql
        assert "variant" in combined_sql
        assert "ev.user_id = e.assignment_id" in EXPERIMENT_METRICS_QUERY
        assert "ev.anonymous_id = e.assignment_id" in EXPERIMENT_METRICS_QUERY
        assert "$experiment_exposure" not in combined_sql
        assert "JSONExtractString(properties, 'experiment_id')" not in combined_sql

    def test_guardrail_query_compares_variants_against_default(self):
        sql = build_feature_flag_frontend_error_guardrail_query(
            exposure_scope_filter="AND page = %(page_scope)s",
            health_scope_filter="AND f.page = %(page_scope)s",
        )

        assert "FROM feature_flag_exposures" in sql
        assert "v.variant AS variant" in sql
        assert "%(default_variant)s AS default_variant" in sql
        assert "WHERE variant = %(default_variant)s" in sql
        assert "JSONExtractString(f.active_flags, %(flag_key)s) = e.variant" in sql
        assert "JSONExtractBool(f.active_flags, %(flag_key)s)" not in sql
        assert " e.value" not in sql
        assert " value" not in sql
