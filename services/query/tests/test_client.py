"""Tests for ClickHouse client query preparation."""

from asynch.proto.connection import Connection

from app.clickhouse.client import normalize_query_params


def test_normalize_query_params_uses_asynch_placeholder_style():
    query = """
SELECT *
FROM events
WHERE project_id = %(project_id)s
  AND event_date BETWEEN %(start_date)s AND %(end_date)s
  AND event_name IN (%(ev_0)s, %(ev_1)s)
"""

    normalized = normalize_query_params(query)

    assert "%(project_id)s" not in normalized
    assert "project_id = {project_id}" in normalized
    assert "event_name IN ({ev_0}, {ev_1})" in normalized


def test_normalized_query_can_be_substituted_by_asynch():
    query = normalize_query_params(
        "SELECT * FROM events WHERE project_id = %(project_id)s "
        "AND event_name = %(event_name)s"
    )

    compiled = Connection.substitute_params(
        query,
        {
            "project_id": "demo",
            "event_name": "$click",
        },
    )

    assert "project_id = 'demo'" in compiled
    assert "event_name = '$click'" in compiled
