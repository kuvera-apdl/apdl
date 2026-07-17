"""Retention analysis endpoint — N-day or N-week retention grid."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Request

from app.auth import require_project
from app.clickhouse.client import ClickHouseClient
from app.clickhouse.queries import build_retention_query
from app.clickhouse.selectors import selector_label
from app.models.schemas import RetentionCohort, RetentionRequest, RetentionResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/query", tags=["retention"])


def _get_client(request: Request) -> ClickHouseClient:
    return request.app.state.ch_client


@router.post("/retention", response_model=RetentionResponse)
async def retention_analysis(
    body: RetentionRequest, request: Request
) -> RetentionResponse:
    """Compute a window-relative N-day or N-week retention grid.

    Each actor enters a cohort on their first ``cohort_selector`` match inside
    the selected dates. Events before the selected dates are not consulted, so
    an existing actor may re-enter on their first in-window match. For each
    cohort, compute the percentage of actors who matched ``return_selector`` on
    each subsequent day or week inside the same selected dates.
    """
    require_project(request, body.project_id, "query:read")
    client = _get_client(request)

    date_key = "cohort_week" if body.period == "week" else "cohort_date"

    params: dict[str, Any] = {
        "project_id": body.project_id,
        "start_date": body.start_date.isoformat(),
        "end_date": body.end_date.isoformat(),
    }
    query = build_retention_query(
        body.cohort_selector,
        body.return_selector,
        params,
        period=body.period,
    )

    rows = await client.execute(query, params)

    # Organise into {cohort_date: {period_offset: active_users, __size: N}}
    cohort_data: dict[str, dict[str, Any]] = defaultdict(lambda: {"__size": 0})

    for row in rows:
        cd = row.get(date_key)
        if hasattr(cd, "isoformat"):
            cd = cd.isoformat()
        cd = str(cd)

        offset = row.get("period_offset")
        active = row.get("active_users", 0)
        size = row.get("cohort_size", 0)

        cohort_data[cd]["__size"] = max(cohort_data[cd]["__size"], size)

        if offset is not None and offset >= 0:
            cohort_data[cd][int(offset)] = active

    # Build the response
    cohorts: list[RetentionCohort] = []
    for cohort_date in sorted(cohort_data.keys()):
        info = cohort_data[cohort_date]
        size = info["__size"]
        if size == 0:
            continue

        # Determine the maximum offset present
        offsets = [k for k in info if isinstance(k, int)]
        max_offset = max(offsets) if offsets else 0

        retention_pcts: list[float] = []
        for offset in range(max_offset + 1):
            active = info.get(offset, 0)
            pct = round(active / size * 100.0, 2) if size > 0 else 0.0
            retention_pcts.append(pct)

        cohorts.append(
            RetentionCohort(
                cohort_date=cohort_date,
                size=size,
                retention=retention_pcts,
            )
        )

    return RetentionResponse(
        cohort_mode=body.cohort_mode,
        cohort_selector=selector_label(body.cohort_selector),
        return_selector=selector_label(body.return_selector),
        cohorts=cohorts,
    )
