"""Feature flag guardrail evaluation endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Request

from app.auth import require_project
from app.clickhouse.client import ClickHouseClient
from app.clickhouse.queries import build_feature_flag_frontend_error_guardrail_query
from app.models.schemas import (
    GuardrailConfig,
    GuardrailEvidence,
    GuardrailEvaluateRequest,
    GuardrailEvaluateResponse,
    GuardrailMetric,
    GuardrailVariantConfig,
)

router = APIRouter(prefix="/v1/query/guardrails", tags=["guardrails"])

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _get_client(request: Request) -> ClickHouseClient:
    return request.app.state.ch_client


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _datetime64_boundary_milliseconds(value: datetime) -> int:
    """Map an instant onto the first DateTime64(3) value at or after it."""
    utc_value = value.astimezone(timezone.utc)
    delta = utc_value - _UNIX_EPOCH
    total_microseconds = (
        (delta.days * 86_400 + delta.seconds) * 1_000_000
        + delta.microseconds
    )
    return -(-total_microseconds // 1_000)


def _millisecond_boundary_isoformat(value_ms: int) -> str:
    value = _UNIX_EPOCH + timedelta(milliseconds=value_ms)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@router.post("/evaluate", response_model=GuardrailEvaluateResponse)
async def evaluate_guardrail_endpoint(
    body: GuardrailEvaluateRequest,
    request: Request,
) -> GuardrailEvaluateResponse:
    """Evaluate one configured feature-flag guardrail without mutating config."""
    require_project(request, body.project_id, "query:read")
    return await evaluate_guardrail(
        _get_client(request),
        project_id=body.project_id,
        flag_key=body.flag_key,
        default_variant=body.default_variant,
        variants=body.variants,
        guardrail=body.guardrail,
    )


async def evaluate_guardrail(
    client: ClickHouseClient,
    *,
    project_id: str,
    flag_key: str,
    default_variant: str,
    variants: list[GuardrailVariantConfig],
    guardrail: GuardrailConfig,
) -> GuardrailEvaluateResponse:
    """Evaluate a frontend health guardrail against ClickHouse data."""
    window_end_ms = _datetime64_boundary_milliseconds(_utc_now())
    window_start_ms = window_end_ms - guardrail.window_minutes * 60_000
    page_scope = _page_scope(guardrail.scope)
    exposure_scope_filter = ""
    health_scope_filter = ""
    params: dict[str, Any] = {
        "project_id": project_id,
        "flag_key": flag_key,
        "default_variant": default_variant,
        "window_start_ms": window_start_ms,
        "window_end_ms": window_end_ms,
    }

    if page_scope is not None:
        exposure_scope_filter = "AND exposure.page = %(page_scope)s"
        health_scope_filter = "AND f.page = %(page_scope)s"
        params["page_scope"] = page_scope

    query = build_feature_flag_frontend_error_guardrail_query(
        exposure_scope_filter=exposure_scope_filter,
        health_scope_filter=health_scope_filter,
    )
    rows = await client.execute(query, params)
    variant_results = _variant_guardrail_results(
        rows,
        default_variant=default_variant,
        variants=variants,
        guardrail=guardrail,
    )
    tripped_result = next(
        (result for result in variant_results if result["tripped"]),
        None,
    )
    evidence = _guardrail_evidence(
        guardrail,
        default_variant=default_variant,
        tripped_result=tripped_result,
        variant_results=variant_results,
        window_start=_millisecond_boundary_isoformat(window_start_ms),
        window_end=_millisecond_boundary_isoformat(window_end_ms),
    )

    return GuardrailEvaluateResponse(
        flag_key=flag_key,
        metric=guardrail.metric.value,
        threshold=guardrail.threshold.value,
        scope=guardrail.scope,
        window_minutes=guardrail.window_minutes,
        tripped=tripped_result is not None,
        evidence=evidence,
    )


def _variant_guardrail_results(
    rows: list[dict[str, Any]],
    *,
    default_variant: str,
    variants: list[GuardrailVariantConfig],
    guardrail: GuardrailConfig,
) -> list[dict[str, Any]]:
    rows_by_variant = {
        str(row.get("variant")): row
        for row in rows
        if row.get("variant") not in (None, "")
    }
    default_counts = _default_counts(rows_by_variant.get(default_variant, {}))

    return [
        _variant_guardrail_result(
            variant.key,
            default_variant=default_variant,
            row={**default_counts, **rows_by_variant.get(variant.key, {})},
            guardrail=guardrail,
        )
        for variant in variants
        if variant.key != default_variant
    ]


def _variant_guardrail_result(
    variant: str,
    *,
    default_variant: str,
    row: dict[str, Any],
    guardrail: GuardrailConfig,
) -> dict[str, Any]:
    variant_sessions = _as_int(row.get("variant_sessions"))
    default_sessions = _as_int(row.get("default_sessions"))
    variant_failure_sessions = _as_int(row.get("variant_failure_sessions"))
    default_failure_sessions = _as_int(row.get("default_failure_sessions"))
    variant_failures = _as_int(row.get("variant_failures"))
    default_failures = _as_int(row.get("default_failures"))

    variant_rate = (
        variant_failure_sessions / variant_sessions if variant_sessions > 0 else 0.0
    )
    default_rate = (
        default_failure_sessions / default_sessions if default_sessions > 0 else 0.0
    )

    if guardrail.metric == GuardrailMetric.frontend_error_count:
        tripped = variant_failures >= 1
    else:
        has_minimum_exposures = (
            variant_sessions >= guardrail.minimum_exposures
            and default_sessions >= guardrail.minimum_exposures
        )
        if not has_minimum_exposures:
            tripped = False
        elif default_rate == 0:
            tripped = variant_rate > 0
        else:
            tripped = variant_rate >= default_rate * 2

    return {
        "variant": variant,
        "default_variant": default_variant,
        "variant_sessions": variant_sessions,
        "default_sessions": default_sessions,
        "variant_failure_sessions": variant_failure_sessions,
        "default_failure_sessions": default_failure_sessions,
        "variant_failures": variant_failures,
        "default_failures": default_failures,
        "variant_error_rate": variant_rate,
        "default_error_rate": default_rate,
        "tripped": tripped,
    }


def _default_counts(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "default_sessions": row.get("default_sessions"),
        "default_failure_sessions": row.get("default_failure_sessions"),
        "default_failures": row.get("default_failures"),
    }


def _guardrail_evidence(
    guardrail: GuardrailConfig,
    *,
    default_variant: str,
    tripped_result: dict[str, Any] | None,
    variant_results: list[dict[str, Any]],
    window_start: str,
    window_end: str,
) -> GuardrailEvidence:
    evidence: dict[str, Any] = {
        "metric": guardrail.metric.value,
        "threshold": guardrail.threshold.value,
        "scope": guardrail.scope,
        "window_minutes": guardrail.window_minutes,
        "window_start": window_start,
        "window_end": window_end,
        "minimum_exposures": guardrail.minimum_exposures,
        "variant": tripped_result["variant"] if tripped_result else None,
        "default_variant": default_variant,
        "variant_results": variant_results,
    }
    if tripped_result is not None:
        evidence.update(tripped_result)
    return GuardrailEvidence.model_validate(evidence)


def _page_scope(scope: str) -> str | None:
    if not scope:
        return None
    return scope.removeprefix("page:")


def _as_int(value: object) -> int:
    if value is None:
        return 0
    return int(value)
