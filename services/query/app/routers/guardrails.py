"""Feature flag guardrail evaluation endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.clickhouse.client import ClickHouseClient
from app.clickhouse.queries import FEATURE_FLAG_FRONTEND_ERROR_GUARDRAIL_QUERY
from app.models.schemas import (
    GuardrailConfig,
    GuardrailEvaluateRequest,
    GuardrailEvaluateResponse,
    GuardrailMetric,
)

router = APIRouter(prefix="/v1/query/guardrails", tags=["guardrails"])


def _get_client(request: Request) -> ClickHouseClient:
    return request.app.state.ch_client


@router.post("/evaluate", response_model=GuardrailEvaluateResponse)
async def evaluate_guardrail_endpoint(
    body: GuardrailEvaluateRequest,
    request: Request,
) -> GuardrailEvaluateResponse:
    """Evaluate one configured feature-flag guardrail without mutating config."""
    return await evaluate_guardrail(
        _get_client(request),
        project_id=body.project_id,
        flag_key=body.flag_key,
        guardrail=body.guardrail,
    )


async def evaluate_guardrail(
    client: ClickHouseClient,
    *,
    project_id: str,
    flag_key: str,
    guardrail: GuardrailConfig,
) -> GuardrailEvaluateResponse:
    """Evaluate a frontend health guardrail against ClickHouse data."""
    page_scope = _page_scope(guardrail.scope)
    exposure_scope_filter = ""
    health_scope_filter = ""
    params: dict[str, Any] = {
        "project_id": project_id,
        "flag_key": flag_key,
        "window_minutes": guardrail.window_minutes,
    }

    if page_scope is not None:
        exposure_scope_filter = "AND page = %(page_scope)s"
        health_scope_filter = "AND f.page = %(page_scope)s"
        params["page_scope"] = page_scope

    query = FEATURE_FLAG_FRONTEND_ERROR_GUARDRAIL_QUERY.format(
        exposure_scope_filter=exposure_scope_filter,
        health_scope_filter=health_scope_filter,
    )
    rows = await client.execute(query, params)
    row = rows[0] if rows else {}

    exposed_sessions = _as_int(row.get("exposed_sessions"))
    baseline_sessions = _as_int(row.get("baseline_sessions"))
    exposed_failure_sessions = _as_int(row.get("exposed_failure_sessions"))
    baseline_failure_sessions = _as_int(row.get("baseline_failure_sessions"))
    exposed_failures = _as_int(row.get("exposed_failures"))
    baseline_failures = _as_int(row.get("baseline_failures"))

    exposed_rate = (
        exposed_failure_sessions / exposed_sessions
        if exposed_sessions > 0
        else 0.0
    )
    baseline_rate = (
        baseline_failure_sessions / baseline_sessions
        if baseline_sessions > 0
        else 0.0
    )

    if guardrail.metric == GuardrailMetric.frontend_error_count:
        tripped = exposed_failures >= 1
    else:
        has_minimum_exposures = (
            exposed_sessions >= guardrail.minimum_exposures
            and baseline_sessions >= guardrail.minimum_exposures
        )
        if not has_minimum_exposures:
            tripped = False
        elif baseline_rate == 0:
            tripped = exposed_rate > 0
        else:
            tripped = exposed_rate >= baseline_rate * 2

    evidence: dict[str, Any] = {
        "metric": guardrail.metric.value,
        "threshold": guardrail.threshold.value,
        "scope": guardrail.scope,
        "window_minutes": guardrail.window_minutes,
        "minimum_exposures": guardrail.minimum_exposures,
        "exposed_sessions": exposed_sessions,
        "baseline_sessions": baseline_sessions,
        "exposed_failure_sessions": exposed_failure_sessions,
        "baseline_failure_sessions": baseline_failure_sessions,
        "exposed_failures": exposed_failures,
        "baseline_failures": baseline_failures,
        "exposed_error_rate": exposed_rate,
        "baseline_error_rate": baseline_rate,
    }

    return GuardrailEvaluateResponse(
        flag_key=flag_key,
        metric=guardrail.metric.value,
        threshold=guardrail.threshold.value,
        scope=guardrail.scope,
        window_minutes=guardrail.window_minutes,
        tripped=tripped,
        evidence=evidence,
    )


def _page_scope(scope: str) -> str | None:
    if not scope:
        return None
    return scope.removeprefix("page:")


def _as_int(value: object) -> int:
    if value is None:
        return 0
    return int(value)
