"""Funnel analysis endpoint — multi-step conversion funnels."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from app.auth import require_project
from app.clickhouse.client import ClickHouseClient
from app.clickhouse.queries import build_funnel_query
from app.clickhouse.selectors import selector_label
from app.models.schemas import FunnelRequest, FunnelResponse, FunnelStep

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/query", tags=["funnels"])


def _get_client(request: Request) -> ClickHouseClient:
    return request.app.state.ch_client


@router.post("/funnel", response_model=FunnelResponse)
async def funnel_analysis(body: FunnelRequest, request: Request) -> FunnelResponse:
    """Compute an N-step conversion funnel.

    Uses ClickHouse's ``windowFunnel`` aggregate function to efficiently
    determine the deepest step each user reached within the specified
    conversion window.
    """
    require_project(request, body.project_id, "query:read")
    if len(body.steps) < 2:
        return FunnelResponse(steps=[], overall_conversion=0.0)

    client = _get_client(request)
    window_seconds = body.window_days * 86400
    params: dict[str, Any] = {
        "project_id": body.project_id,
        "start_date": body.start_date.isoformat(),
        "end_date": body.end_date.isoformat(),
    }
    query = build_funnel_query(body.steps, params, window_seconds=window_seconds)

    rows = await client.execute(query, params)

    # Build a mapping from step_number -> user count
    step_counts: dict[int, int] = {}
    for row in rows:
        step_num = int(row["step_number"])
        step_counts[step_num] = int(row["users"])

    # Assemble the response
    num_steps = len(body.steps)
    funnel_steps: list[FunnelStep] = []
    step_1_count = step_counts.get(1, 0)

    for i in range(1, num_steps + 1):
        count = step_counts.get(i, 0)
        prev_count = step_counts.get(i - 1, step_1_count) if i > 1 else count

        conversion_rate = (count / prev_count * 100.0) if prev_count > 0 else 0.0
        overall_rate = (count / step_1_count * 100.0) if step_1_count > 0 else 0.0

        if i == 1:
            conversion_rate = 100.0

        funnel_steps.append(
            FunnelStep(
                step=i,
                event_name=body.steps[i - 1].event_name,
                selector=selector_label(body.steps[i - 1]),
                count=count,
                conversion_rate=round(conversion_rate, 2),
                overall_rate=round(overall_rate, 2),
            )
        )

    last_count = step_counts.get(num_steps, 0)
    overall_conversion = (
        (last_count / step_1_count * 100.0) if step_1_count > 0 else 0.0
    )

    return FunnelResponse(
        steps=funnel_steps,
        overall_conversion=round(overall_conversion, 2),
    )
