"""Unit tests for selector SQL generation and analytics query builders."""

import re
from datetime import datetime
from pathlib import Path

import pytest
from asynch.proto.utils.escape import escape_params
from pydantic import ValidationError

from app.clickhouse.client import normalize_query_params
from app.clickhouse.queries import (
    EXPERIMENT_ANALYSIS_QUERY,
    build_cohort_query,
    build_event_catalog_query,
    build_event_count_query,
    build_feature_flag_frontend_error_guardrail_query,
    build_funnel_query,
    build_retention_query,
)
from app.clickhouse.selectors import build_selector_condition, selector_label
from app.models.schemas import EventSelector
from app.routers.experiments import _datetime64_boundary_milliseconds


QUERIES_SOURCE = (
    Path(__file__).resolve().parents[1] / "app" / "clickhouse" / "queries.py"
).read_text()


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
    def test_replacing_table_reads_always_apply_final(self):
        replacing_tables = {
            "events",
            "feature_flag_exposures",
            "frontend_health_events",
        }
        table_read = re.compile(r"\b(?:FROM|JOIN)\s+([a-z_]+)\b")

        reads = []
        for line in QUERIES_SOURCE.splitlines():
            match = table_read.search(line)
            if match and match.group(1) in replacing_tables:
                reads.append(line.strip())

        assert reads
        assert all(read.endswith(" FINAL") for read in reads)

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
        assert "1 AS is_total" in sql
        assert "uniqExactIf" in sql
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
        assert "concat('u:', user_id)" in sql
        assert "concat('a:', anonymous_id)" in sql

    def test_non_experiment_analytics_share_namespaced_actor_identity(self):
        params = {}
        queries = [
            build_event_count_query([_selector("page")], params),
            build_event_catalog_query(params),
            build_cohort_query(_selector("purchase"), params),
            build_retention_query(
                _selector("signup"),
                _selector("return"),
                params,
                period="day",
            ),
        ]

        for sql in queries:
            assert "concat('u:', user_id)" in sql
            assert "concat('a:', anonymous_id)" in sql

    def test_guardrail_uses_session_then_namespaced_actor_fallback(self):
        sql = build_feature_flag_frontend_error_guardrail_query()

        assert "concat('s:', session_id)" in sql
        assert "concat('u:', user_id)" in sql
        assert "concat('a:', anonymous_id)" in sql
        assert "concat('s:', f.session_id)" in sql

    def test_experiment_query_is_exposure_led_and_first_assignment_wins(self):
        sql = EXPERIMENT_ANALYSIS_QUERY

        assert "FROM feature_flag_exposures FINAL" in sql
        assert "flag_key = %(flag_key)s" in sql
        assert "argMin(variant, tuple(first_exposure, message_id))" in sql
        assert "uniqExact(variant) > 1 AS crossed_over" in sql
        assert "LEFT JOIN metric_events" in sql
        assert "countIf(converted) AS conversions" in sql
        assert "INNER JOIN" not in sql

    def test_experiment_query_namespaces_identities_without_stitching(self):
        sql = EXPERIMENT_ANALYSIS_QUERY

        assert sql.count("concat('u:', user_id)") >= 2
        assert sql.count("concat('a:', anonymous_id)") >= 2
        assert "user_id =" not in sql
        assert "anonymous_id =" not in sql
        assert " OR " not in sql

    def test_experiment_query_uses_authoritative_metric_and_half_open_window(self):
        sql = EXPERIMENT_ANALYSIS_QUERY

        assert "event_name = %(metric_event)s" in sql
        assert "fromUnixTimestamp64Milli(%(start_ms)s, 'UTC')" in sql
        assert "fromUnixTimestamp64Milli(%(end_ms)s, 'UTC')" in sql
        assert "first_exposure >= analysis_start" in sql
        assert "first_exposure < analysis_end" in sql
        assert "timestamp >= analysis_start" in sql
        assert "timestamp < analysis_end" in sql
        assert sql.count("event_date BETWEEN toDate(analysis_start)") == 2
        assert "%(start_date)s" not in sql
        assert "%(end_date)s" not in sql
        assert "%(metric)s" not in sql
        assert "$experiment_exposure" not in sql
        assert "experiment_id" not in sql

    def test_experiment_window_keeps_fractional_offset_precision_through_driver(self):
        start = datetime.fromisoformat("2025-01-01T01:00:00.123000+01:00")
        end = datetime.fromisoformat("2025-01-01T01:00:00.123456+01:00")
        params = {
            "start_ms": _datetime64_boundary_milliseconds(start),
            "end_ms": _datetime64_boundary_milliseconds(end),
        }

        assert params == {
            "start_ms": 1_735_689_600_123,
            "end_ms": 1_735_689_600_124,
        }
        rendered = normalize_query_params(
            "SELECT fromUnixTimestamp64Milli(%(start_ms)s, 'UTC'), "
            "fromUnixTimestamp64Milli(%(end_ms)s, 'UTC')"
        ).format(**escape_params(params))
        assert rendered == (
            "SELECT fromUnixTimestamp64Milli(1735689600123, 'UTC'), "
            "fromUnixTimestamp64Milli(1735689600124, 'UTC')"
        )

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
