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
    EXPERIMENT_PROVENANCE_QUERY,
    build_cohort_query,
    build_event_breakdown_query,
    build_event_catalog_query,
    build_event_count_query,
    build_event_timeseries_query,
    build_feature_flag_frontend_error_guardrail_query,
    build_funnel_query,
    build_retention_query,
    canonical_actor_sql,
    identity_alias_join_sql,
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
        assert "JSONType(properties, %(unit_filter_0_property)s) = 'String'" in sql
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

        assert "JSONType(properties, %(unit_filter_0_property)s) = 'String'" in sql
        assert "position(JSONExtractString" in sql
        assert "positionCaseSensitive" not in sql
        assert params["unit_filter_0_value"] == "Start"

    def test_in_filter_uses_parameterized_value_list(self):
        selector = _selector(
            "signup",
            [{"property": "plan", "operator": "in", "value": ["pro", "team"]}],
        )
        params = {}

        sql = build_selector_condition(selector, params, "unit")

        assert "JSONType(properties, %(unit_filter_0_property)s) = 'String'" in sql
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

        assert (
            "JSONType(properties, %(unit_filter_0_property)s) "
            "IN ('Int64', 'UInt64', 'Double')"
        ) in sql
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

    def test_boolean_filter_requires_boolean_json_type(self):
        selector = _selector(
            "checkout",
            [{"property": "is_primary", "operator": "eq", "value": True}],
        )
        params = {}

        sql = build_selector_condition(selector, params, "unit")

        assert "JSONType(properties, %(unit_filter_0_property)s) = 'Bool'" in sql
        assert "JSONExtractBool(properties, %(unit_filter_0_property)s)" in sql
        assert params["unit_filter_0_value"] == 1

    @pytest.mark.parametrize(
        ("operator", "value"),
        [
            ("eq", "pro"),
            ("neq", "pro"),
            ("in", ["pro", "team"]),
            ("not_in", ["pro", "team"]),
            ("contains", "pro"),
            ("gt", 1),
            ("gte", 1),
            ("lt", 1),
            ("lte", 1),
        ],
    )
    def test_every_typed_operator_guards_json_type(self, operator, value):
        selector = _selector(
            "checkout",
            [{"property": "subject", "operator": operator, "value": value}],
        )

        sql = build_selector_condition(selector, {}, "unit")

        assert "JSONType(properties, %(unit_filter_0_property)s)" in sql

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
                [
                    {
                        "property": "href); DROP TABLE events",
                        "operator": "eq",
                        "value": "/",
                    }
                ],
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
        physical_provenance_scans = {
            "FROM feature_flag_exposures AS provenance_exposure",
            "FROM events AS provenance_metric",
        }
        assert all(
            read.endswith(" FINAL") or read in physical_provenance_scans
            for read in reads
        )

    def test_count_query_returns_one_row_per_selector(self):
        selectors = [
            _selector(
                "$click", [{"property": "href", "operator": "eq", "value": "/a"}]
            ),
            _selector(
                "$click", [{"property": "href", "operator": "eq", "value": "/b"}]
            ),
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
        assert "concat('u:', e.user_id)" in sql
        assert "concat('u:', actor_identity.resolved_user_id)" in sql
        assert "concat('a:', e.anonymous_id)" in sql

    def test_all_actor_analytics_use_tenant_scoped_fail_closed_aliases(self):
        queries = [
            build_event_count_query([_selector("page")], {}),
            build_event_timeseries_query(_selector("page"), {}, "1 DAY"),
            build_event_breakdown_query(_selector("page"), {}),
            build_event_catalog_query({}),
            build_cohort_query(_selector("purchase"), {}),
            build_funnel_query([_selector("page"), _selector("purchase")], {}),
            build_retention_query(
                _selector("signup"),
                _selector("return"),
                {},
                period="day",
            ),
            build_retention_query(
                _selector("signup"),
                _selector("return"),
                {},
                period="week",
            ),
        ]

        for sql in queries:
            assert "FROM resolved_identity_aliases" in sql
            assert "WHERE project_id = %(project_id)s" in sql
            assert "has_conflict" in sql
            assert "resolved_user_id" in sql
            assert "concat('a:'," in sql

    def test_actor_resolution_prefers_direct_user_then_alias_then_anonymous(self):
        actor = canonical_actor_sql("event", "actor_identity")

        direct_user = "event.user_id != '', concat('u:', event.user_id)"
        resolved_user = (
            "actor_identity.resolved_user_id != '', "
            "concat('u:', actor_identity.resolved_user_id)"
        )
        anonymous = "concat('a:', event.anonymous_id)"
        assert (
            actor.index(direct_user)
            < actor.index(resolved_user)
            < actor.index(anonymous)
        )

    def test_alias_join_is_tenant_bound_fail_closed_and_retroactive(self):
        join = identity_alias_join_sql("event", "actor_identity")

        assert "FROM resolved_identity_aliases" in join
        assert "WHERE project_id = %(project_id)s" in join
        assert "resolved_user_id, has_conflict" in join
        assert "AND has_conflict = 0" not in join
        assert "actor_identity.project_id = event.project_id" in join
        assert "actor_identity.anonymous_id = event.anonymous_id" in join
        assert "first_identified_at" not in join
        assert "last_identified_at" not in join

    def test_guardrail_uses_session_then_namespaced_actor_fallback(self):
        sql = build_feature_flag_frontend_error_guardrail_query()

        assert "concat('s:', exposure.session_id)" in sql
        assert "concat('u:', exposure.user_id)" in sql
        assert "concat('u:', exposure_identity.resolved_user_id)" in sql
        assert "concat('a:', exposure.anonymous_id)" in sql
        assert "concat('s:', f.session_id)" in sql
        assert "concat('u:', health_identity.resolved_user_id)" in sql
        assert sql.count("FROM resolved_identity_aliases") == 2

    def test_experiment_query_is_exposure_led_and_first_assignment_wins(self):
        sql = EXPERIMENT_ANALYSIS_QUERY

        assert "FROM boundary_events AS exposure" in sql
        assert "FROM feature_flag_exposures AS exposure" not in sql
        assert "FROM experiment_event_deliveries FINAL" in sql
        assert "FROM boundary_events AS metric" in sql
        assert "LIMIT 1 BY project_id, message_id" in sql
        assert "JSONExtractString(exposure.properties, 'flag_key')" in sql
        assert "JSONExtractUInt(exposure.properties, 'config_version')" in sql
        assert "JSONExtractString(exposure.properties, 'reason')" in sql
        assert "argMin(variant, tuple(first_exposure, message_id))" in sql
        assert "uniqExact(variant) > 1 AS crossed_over" in sql
        assert "variant NOT IN %(declared_variants)s" in sql
        assert "countIf(has_unknown_variant) AS unknown_variant_actors" in sql
        assert "LEFT JOIN metric_events" in sql
        assert "countIf(converted) AS conversions" in sql
        assert "LEFT JOIN metric_events" in sql

    def test_experiment_query_stitches_exposures_and_metrics_through_same_aliases(self):
        sql = EXPERIMENT_ANALYSIS_QUERY

        assert sql.count("FROM experiment_event_deliveries FINAL") == 1
        assert sql.count("FROM boundary_events") == 4
        assert "FROM boundary_events AS metric" in sql
        assert sql.count("%(boundary_stream_id_ms)s") >= 2
        assert sql.count("%(boundary_stream_id_seq)s") == 1
        assert "concat('u:', exposure_identity.resolved_user_id)" in sql
        assert "concat('u:', metric_identity.resolved_user_id)" in sql
        assert "exposure_identity.anonymous_id = exposure.anonymous_id" in sql
        assert "metric_identity.anonymous_id = metric.anonymous_id" in sql
        assert "ifNull(exposure_identity.has_conflict, 0)" in sql
        assert "ifNull(metric_identity.has_conflict, 0)" in sql
        assert "uniqExact(actor_id) AS identity_conflict_actors" in sql
        assert "INNER JOIN assignments AS assignment" in sql
        assert "assignment.actor_id = metric.actor_id" in sql
        assert "metric.timestamp >= assignment.assigned_at" in sql
        assert "CROSS JOIN identity_quality" in sql
        assert "exposure.user_id = metric.user_id" not in sql
        assert "exposure.anonymous_id = metric.anonymous_id" not in sql

    def test_experiment_provenance_keeps_superseded_unprovenanced_rows_visible(self):
        sql = EXPERIMENT_PROVENANCE_QUERY

        # FINAL could hide a pre-boundary, pre-provenance row after a later
        # replacement of the same client message ID. Completeness must retain
        # that uncertainty and fail closed rather than bless the replacement.
        assert "FROM feature_flag_exposures FINAL" not in sql
        assert "FROM events FINAL" not in sql
        assert "FROM identity_alias_assertions FINAL" not in sql
        assert "FROM experiment_event_deliveries FINAL" not in sql
        assert "FROM feature_flag_exposures AS provenance_exposure" in sql
        assert "FROM events AS provenance_metric" in sql
        assert "FROM identity_alias_assertions AS provenance_identity" in sql
        assert "FROM experiment_event_deliveries AS provenance_delivery" in sql

    def test_experiment_query_uses_authoritative_metric_and_half_open_window(self):
        sql = EXPERIMENT_ANALYSIS_QUERY

        assert "metric.event_name = %(metric_event)s" in sql
        assert "fromUnixTimestamp64Milli(%(start_ms)s, 'UTC')" in sql
        assert "fromUnixTimestamp64Milli(%(end_ms)s, 'UTC')" in sql
        assert "exposure.timestamp >= analysis_start" in sql
        assert "exposure.timestamp < analysis_end" in sql
        assert "metric.timestamp >= analysis_start" in sql
        assert "metric.timestamp < analysis_end" in sql
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
            exposure_scope_filter="AND exposure.page = %(page_scope)s",
            health_scope_filter="AND f.page = %(page_scope)s",
        )

        assert "FROM feature_flag_exposures" in sql
        assert "fromUnixTimestamp64Milli(%(window_start_ms)s, 'UTC')" in sql
        assert "fromUnixTimestamp64Milli(%(window_end_ms)s, 'UTC')" in sql
        assert "exposure.first_exposure >= window_start" in sql
        assert "exposure.first_exposure < window_end" in sql
        assert "f.timestamp >= window_start" in sql
        assert "f.timestamp < window_end" in sql
        assert (
            sql.count("event_date BETWEEN toDate(window_start) AND toDate(window_end)")
            == 2
        )
        assert "exposure.page = %(page_scope)s" in sql
        assert "f.page = %(page_scope)s" in sql
        assert "now()" not in sql
        assert "%(window_minutes)s" not in sql
        assert "v.variant AS variant" in sql
        assert "%(default_variant)s AS default_variant" in sql
        assert "WHERE variant = %(default_variant)s" in sql
        assert "JSONExtractString(f.active_flags, %(flag_key)s) = e.variant" in sql
        health_cte, join_clause = sql.split("    exposure_failures AS (", 1)
        assert "f.event_name = '$frontend_error'" in health_cte
        assert "JSONHas(f.active_flags, %(flag_key)s)" in health_cte
        assert "f.event_name = '$frontend_error'" not in join_clause
        assert "JSONHas(f.active_flags, %(flag_key)s)" not in join_clause
        assert "JSONExtractBool(f.active_flags, %(flag_key)s)" not in sql
        assert " e.value" not in sql
        assert " value" not in sql
