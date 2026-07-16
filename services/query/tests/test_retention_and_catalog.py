"""Retention SQL is ClickHouse-compatible, and the event-catalog endpoint works."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.clickhouse.queries import (
    build_event_catalog_query,
    build_event_count_query,
    build_retention_query,
)
from app.main import app
from app.models.schemas import EventSelector


def _sel(name: str) -> EventSelector:
    return EventSelector(event_name=name, filters=[])


# ---------------------------------------------------------------------------
# Retention query builder — must not emit an inequality JOIN ON condition,
# which ClickHouse rejects (Code 403: Unsupported JOIN ON conditions).
# ---------------------------------------------------------------------------


def test_retention_day_query_is_clickhouse_compatible():
    sql = build_retention_query(_sel("page"), _sel("$click"), {}, period="day")
    assert "cohort_sizes" in sql
    assert "INNER JOIN cohort_sizes" in sql
    # The forbidden inequality-in-JOIN-ON is gone; direction is filtered app-side.
    assert ">=" not in sql


def test_retention_week_query_is_clickhouse_compatible():
    sql = build_retention_query(_sel("page"), _sel("$click"), {}, period="week")
    assert "cohort_sizes" in sql
    assert ">=" not in sql


def test_event_catalog_query_shape():
    sql = build_event_catalog_query({})
    assert "GROUP BY e.event_name" in sql
    assert "ORDER BY event_count DESC" in sql
    assert "LIMIT %(limit)s" in sql


def test_event_count_qualifies_event_name_to_avoid_alias_shadow():
    # The literal label is aliased AS event_name; if the filter referenced the
    # bare column, ClickHouse would bind it to that alias and every selector
    # would match all events (returning the grand total). The filter must
    # target the real column, e.event_name.
    sql = build_event_count_query([_sel("page"), _sel("$click")], {})
    assert sql.count("e.event_name =") == 4
    assert "AS event_name" in sql
    assert "1 AS is_total" in sql


# ---------------------------------------------------------------------------
# /v1/query/events/names endpoint
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_ch():
    mock = AsyncMock()
    mock.execute = AsyncMock(return_value=[])
    app.state.ch_client = mock
    yield


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_event_names_endpoint(client):
    app.state.ch_client.execute = AsyncMock(
        return_value=[
            {"event_name": "$click", "event_count": 79, "unique_users": 3},
            {"event_name": "page", "event_count": 57, "unique_users": 3},
        ]
    )
    resp = await client.post(
        "/v1/query/events/names",
        json={
            "project_id": "apiasport",
            "start_date": "2026-06-14",
            "end_date": "2026-06-21",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [e["event_name"] for e in body["events"]] == ["$click", "page"]


@pytest.mark.asyncio
async def test_event_names_rejects_bad_date_range(client):
    resp = await client.post(
        "/v1/query/events/names",
        json={
            "project_id": "apiasport",
            "start_date": "2026-06-21",
            "end_date": "2026-06-14",
        },
    )
    assert resp.status_code == 422
