"""Cohort comparison endpoint — segment users by a property and compare metric over time."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Request

from app.auth import require_project
from app.clickhouse.client import ClickHouseClient
from app.clickhouse.queries import build_cohort_query
from app.clickhouse.selectors import selector_label
from app.models.schemas import CohortRequest, CohortResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/query", tags=["cohorts"])


def _get_client(request: Request) -> ClickHouseClient:
    return request.app.state.ch_client


@router.post("/cohort", response_model=CohortResponse)
async def cohort_comparison(body: CohortRequest, request: Request) -> CohortResponse:
    """Compare metric performance across user cohorts defined by a property value.

    Each cohort is a distinct value of ``cohort_property`` extracted from
    event properties.  The response contains per-day event counts and
    unique users for each cohort.
    """
    require_project(request, body.project_id, "query:read")
    client = _get_client(request)

    params: dict[str, Any] = {
        "project_id": body.project_id,
        "cohort_property": body.cohort_property,
        "start_date": body.start_date.isoformat(),
        "end_date": body.end_date.isoformat(),
    }
    query = build_cohort_query(body.metric_selector, params)

    rows = await client.execute(query, params)

    # Group rows by cohort_value
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cohort_val = row.get("cohort_value", "unknown")
        day = row.get("day")
        if hasattr(day, "isoformat"):
            day = day.isoformat()
        grouped[cohort_val].append(
            {
                "day": day,
                "event_count": row.get("event_count", 0),
                "unique_users": row.get("unique_users", 0),
            }
        )

    cohorts = []
    for cohort_value, timeseries in sorted(grouped.items()):
        total_events = sum(p["event_count"] for p in timeseries)
        total_users = sum(p["unique_users"] for p in timeseries)
        cohorts.append(
            {
                "cohort_value": cohort_value,
                "total_events": total_events,
                "total_users": total_users,
                "timeseries": timeseries,
            }
        )

    return CohortResponse(
        metric_selector=selector_label(body.metric_selector),
        cohort_property=body.cohort_property,
        cohorts=cohorts,
    )
