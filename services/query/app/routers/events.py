"""Event query endpoints — counts, timeseries, and property breakdowns."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from app.auth import require_project
from app.clickhouse.client import ClickHouseClient
from app.clickhouse.queries import (
    build_event_breakdown_query,
    build_event_catalog_query,
    build_event_count_query,
    build_event_timeseries_query,
)
from app.clickhouse.selectors import selector_label
from app.models.schemas import (
    BreakdownRequest,
    BreakdownResponse,
    EventCatalogRequest,
    EventCatalogResponse,
    EventCountRequest,
    EventCountResponse,
    TimeseriesRequest,
    TimeseriesResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/query/events", tags=["events"])


def _get_client(request: Request) -> ClickHouseClient:
    return request.app.state.ch_client


@router.post("/count", response_model=EventCountResponse)
async def event_counts(body: EventCountRequest, request: Request) -> EventCountResponse:
    """Aggregate event counts and unique-user counts for event selectors."""
    require_project(request, body.project_id, "query:read")
    client = _get_client(request)

    params: dict[str, Any] = {
        "project_id": body.project_id,
        "start_date": body.start_date.isoformat(),
        "end_date": body.end_date.isoformat(),
    }

    query = build_event_count_query(body.selectors, params)
    rows = await client.execute(query, params)

    total_events = sum(r.get("event_count", 0) for r in rows)
    # Unique users across events requires a separate uniq, but as a pragmatic
    # approximation we take the max unique_users across rows (for the overview)
    # or sum them (which over-counts).  For accuracy we'd run a dedicated query.
    # Here we return the sum-of-unique which is an upper bound.
    total_users = sum(r.get("unique_users", 0) for r in rows)

    return EventCountResponse(
        results=rows, total_events=total_events, total_users=total_users
    )


@router.post("/timeseries", response_model=TimeseriesResponse)
async def event_timeseries(
    body: TimeseriesRequest, request: Request
) -> TimeseriesResponse:
    """Time-bucketed event counts for one event selector."""
    require_project(request, body.project_id, "query:read")
    client = _get_client(request)

    # The interval value (e.g. "1 DAY") is injected directly into the SQL
    # because ClickHouse does not support parameterised INTERVAL literals.
    # The TimeInterval enum constrains input to known-safe values.

    params: dict[str, Any] = {
        "project_id": body.project_id,
        "start_date": body.start_date.isoformat(),
        "end_date": body.end_date.isoformat(),
    }
    query = build_event_timeseries_query(body.selector, params, body.interval.value)

    rows = await client.execute(query, params)

    # Normalise datetime objects to ISO strings for JSON serialisation
    buckets = []
    for row in rows:
        bucket = dict(row)
        if "bucket" in bucket and hasattr(bucket["bucket"], "isoformat"):
            bucket["bucket"] = bucket["bucket"].isoformat()
        buckets.append(bucket)

    return TimeseriesResponse(selector=selector_label(body.selector), buckets=buckets)


@router.post("/breakdown", response_model=BreakdownResponse)
async def event_breakdown(
    body: BreakdownRequest, request: Request
) -> BreakdownResponse:
    """Break down a selector's matching events by a JSON property value."""
    require_project(request, body.project_id, "query:read")
    client = _get_client(request)

    params: dict[str, Any] = {
        "project_id": body.project_id,
        "property": body.property,
        "start_date": body.start_date.isoformat(),
        "end_date": body.end_date.isoformat(),
        "limit": body.limit,
    }
    query = build_event_breakdown_query(body.selector, params)

    rows = await client.execute(query, params)
    return BreakdownResponse(
        selector=selector_label(body.selector),
        property=body.property,
        results=rows,
    )


@router.post("/names", response_model=EventCatalogResponse)
async def event_names(
    body: EventCatalogRequest, request: Request
) -> EventCatalogResponse:
    """List the event names present for a project, most frequent first.

    Powers agent event discovery so analysis plans target events that exist.
    """
    require_project(request, body.project_id, "query:read")
    client = _get_client(request)

    params: dict[str, Any] = {
        "project_id": body.project_id,
        "start_date": body.start_date.isoformat(),
        "end_date": body.end_date.isoformat(),
        "limit": body.limit,
    }
    query = build_event_catalog_query(params)
    rows = await client.execute(query, params)
    return EventCatalogResponse(events=rows)
