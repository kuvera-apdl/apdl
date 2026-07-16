"""Focused contract tests for typed event-property breakdown SQL."""

from app.clickhouse.queries import build_event_breakdown_query
from app.models.schemas import EventSelector


def _build_query() -> tuple[str, dict[str, object]]:
    params: dict[str, object] = {
        "project_id": "demo",
        "property": "value",
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
        "limit": 20,
    }
    query = build_event_breakdown_query(
        EventSelector(event_name="purchase"),
        params,
    )
    return query, params


def test_breakdown_keeps_json_scalar_types_in_separate_buckets():
    query, params = _build_query()

    assert "JSONType(e.properties, %(property)s) AS property_json_type" in query
    assert "property_json_type = 'String', 'string'" in query
    assert "property_json_type IN ('Int64', 'UInt64'), 'integer'" in query
    assert "property_json_type = 'Double', 'float'" in query
    assert "property_json_type = 'Bool', 'boolean'" in query
    assert "GROUP BY selector, property_type, property_value" in query
    assert params["breakdown_event_name"] == "purchase"


def test_breakdown_uses_canonical_text_for_each_scalar_type():
    query, _ = _build_query()

    assert "JSONExtractString(properties, %(property)s)" in query
    assert "toString(JSONExtractInt(properties, %(property)s))" in query
    assert "toString(JSONExtractUInt(properties, %(property)s))" in query
    assert "toString(JSONExtractFloat(properties, %(property)s))" in query
    assert (
        "if(JSONExtractBool(properties, %(property)s), 'true', 'false')" in query
    )


def test_breakdown_excludes_non_scalar_and_missing_values():
    query, _ = _build_query()

    allowed_types = "IN ('String', 'Int64', 'UInt64', 'Double', 'Bool')"
    assert allowed_types in query
    assert "'Array'" not in query
    assert "'Object'" not in query
    assert "'Null'" not in query


def test_breakdown_preserves_tenant_bound_identity_resolution():
    query, _ = _build_query()

    assert "FROM resolved_identity_aliases" in query
    assert "WHERE project_id = %(project_id)s" in query
    assert "actor_identity.project_id = e.project_id" in query
    assert "actor_identity.anonymous_id = e.anonymous_id" in query
    assert "uniqExactIf(actor_id, actor_id != '')" in query
