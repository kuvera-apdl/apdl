"""ClickHouse query tools for the Query Service HTTP API."""

from __future__ import annotations

import os
from typing import Any, Literal, NotRequired, TypeAlias, TypedDict

import httpx

from app.service_auth import service_headers

QUERY_SERVICE_URL = os.getenv("QUERY_SERVICE_URL", "http://localhost:8082")
_TIMEOUT = 30.0

FilterOperator: TypeAlias = Literal[
    "eq",
    "neq",
    "in",
    "not_in",
    "exists",
    "not_exists",
    "contains",
    "gt",
    "gte",
    "lt",
    "lte",
]
FilterScalar: TypeAlias = str | int | float | bool
FilterValue: TypeAlias = FilterScalar | list[FilterScalar]


class EventPropertyFilterPayload(TypedDict):
    property: str
    operator: FilterOperator
    value: NotRequired[FilterValue]


class EventSelectorPayload(TypedDict):
    event_name: str
    filters: list[EventPropertyFilterPayload]


async def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to the query service and return the JSON response."""
    async with httpx.AsyncClient(base_url=QUERY_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.post(
            path,
            json=payload,
            headers=service_headers(str(payload["project_id"])),
        )
        resp.raise_for_status()
        return resp.json()


async def query_events(
    project_id: str,
    start_date: str,
    end_date: str,
    selectors: list[EventSelectorPayload],
) -> dict[str, Any]:
    """Query aggregated event counts and unique users.

    Args:
        project_id: The project to query.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        selectors: Event selectors to count.
    """
    return await _post("/v1/query/events/count", {
        "project_id": project_id,
        "start_date": start_date,
        "end_date": end_date,
        "selectors": selectors,
    })


async def discover_events(
    project_id: str,
    start_date: str,
    end_date: str,
    limit: int = 100,
) -> dict[str, Any]:
    """List the event names present for a project, most frequent first.

    Lets the behaviour agent plan queries against events that actually exist
    instead of guessing names.

    Args:
        project_id: The project to inspect.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        limit: Max distinct event names to return.
    """
    return await _post("/v1/query/events/names", {
        "project_id": project_id,
        "start_date": start_date,
        "end_date": end_date,
        "limit": limit,
    })


async def query_timeseries(
    project_id: str,
    selector: EventSelectorPayload,
    start_date: str,
    end_date: str,
    interval: str = "1 DAY",
) -> dict[str, Any]:
    """Query time-bucketed event counts for a single event.

    Args:
        project_id: The project to query.
        selector: Event selector to chart.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        interval: Bucket interval — "1 HOUR", "1 DAY", "1 WEEK", or "1 MONTH".
    """
    # Allowlist at this boundary: interval is often LLM-authored and plausibly
    # ends up interpolated into SQL downstream in the query service.
    allowed = {"1 HOUR", "1 DAY", "1 WEEK", "1 MONTH"}
    if interval.upper() not in allowed:
        interval = "1 DAY"
    else:
        interval = interval.upper()
    return await _post("/v1/query/events/timeseries", {
        "project_id": project_id,
        "selector": selector,
        "start_date": start_date,
        "end_date": end_date,
        "interval": interval,
    })


async def query_funnel(
    project_id: str,
    steps: list[EventSelectorPayload],
    start_date: str,
    end_date: str,
    window_days: int = 7,
) -> dict[str, Any]:
    """Run a multi-step funnel analysis.

    Args:
        project_id: The project to query.
        steps: Ordered list of event selectors defining the funnel.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        window_days: Max days between first and last step.
    """
    return await _post("/v1/query/funnel", {
        "project_id": project_id,
        "steps": steps,
        "start_date": start_date,
        "end_date": end_date,
        "window_days": window_days,
    })


async def query_retention(
    project_id: str,
    cohort_selector: EventSelectorPayload,
    return_selector: EventSelectorPayload,
    start_date: str,
    end_date: str,
    period: str = "day",
) -> dict[str, Any]:
    """Compute an N-day or N-week retention grid.

    Args:
        project_id: The project to query.
        cohort_selector: Selector that defines the cohort (first occurrence).
        return_selector: Selector to check for retention.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        period: "day" or "week".
    """
    return await _post("/v1/query/retention", {
        "project_id": project_id,
        "cohort_selector": cohort_selector,
        "return_selector": return_selector,
        "start_date": start_date,
        "end_date": end_date,
        "period": period,
    })


async def query_cohort(
    project_id: str,
    cohort_property: str,
    metric_selector: EventSelectorPayload,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Compare metrics across user cohorts defined by a property value.

    Args:
        project_id: The project to query.
        cohort_property: JSON property to segment users by.
        metric_selector: Event selector to measure across cohorts.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
    """
    return await _post("/v1/query/cohort", {
        "project_id": project_id,
        "cohort_property": cohort_property,
        "metric_selector": metric_selector,
        "start_date": start_date,
        "end_date": end_date,
    })


async def query_breakdown(
    project_id: str,
    selector: EventSelectorPayload,
    property_name: str,
    start_date: str,
    end_date: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Break down an event by a JSON property value.

    Args:
        project_id: The project to query.
        selector: Event selector to analyse.
        property_name: JSON property key to break down by.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        limit: Max number of distinct values to return.
    """
    return await _post("/v1/query/events/breakdown", {
        "project_id": project_id,
        "selector": selector,
        "property": property_name,
        "start_date": start_date,
        "end_date": end_date,
        "limit": limit,
    })
