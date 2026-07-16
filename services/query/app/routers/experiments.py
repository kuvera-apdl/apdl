"""Authoritative, exposure-led experiment analysis."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query, Request

from app.auth import require_project
from app.clickhouse.client import ClickHouseClient
from app.clickhouse.queries import EXPERIMENT_ANALYSIS_QUERY
from app.config_client import (
    ConfigExperimentAnalysis,
    ConfigServiceUnavailable,
    ExperimentNotAnalyzable,
    ExperimentNotFound,
    fetch_experiment_analysis,
)
from app.models.schemas import (
    ExperimentAnalysisInsufficient,
    ExperimentAnalysisReady,
    ExperimentAnalysisResponse,
    ExperimentArmResult,
    ExperimentComparison,
)

router = APIRouter(prefix="/v1/query", tags=["experiments"])

_ALPHA = 0.05
_MIN_SAMPLE_SIZE_PER_ARM = 2
_MAX_EXPERIMENT_WINDOW = timedelta(days=90)
_ALLOWED_QUERY_PARAMETERS = frozenset({"project_id"})
_RESOURCE_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _get_client(request: Request) -> ClickHouseClient:
    return request.app.state.ch_client


def _reject_unknown_query_parameters(request: Request) -> None:
    unknown = sorted(set(request.query_params) - _ALLOWED_QUERY_PARAMETERS)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown query parameter(s): {', '.join(unknown)}",
        )
    if len(request.query_params.getlist("project_id")) > 1:
        raise HTTPException(
            status_code=422,
            detail="project_id must be supplied at most once",
        )


def _empty_arms(metadata: ConfigExperimentAnalysis) -> list[ExperimentArmResult]:
    return [
        ExperimentArmResult(
            variant=variant,
            sample_size=0,
            conversions=0,
            conversion_rate=0.0,
        )
        for variant in metadata.variants
    ]


def _datetime64_boundary_milliseconds(value: datetime) -> int:
    """Map an exact instant onto the first DateTime64(3) value at/after it."""
    utc_value = value.astimezone(timezone.utc)
    delta = utc_value - _UNIX_EPOCH
    total_microseconds = (
        (delta.days * 86_400 + delta.seconds) * 1_000_000
        + delta.microseconds
    )
    return -(-total_microseconds // 1_000)


def _aggregate_arms(
    metadata: ConfigExperimentAnalysis,
    rows: list[dict[str, Any]],
) -> tuple[list[ExperimentArmResult], int, int]:
    declared = set(metadata.variants)
    aggregates: dict[str, tuple[int, int]] = {}
    seen_variants: set[str] = set()
    crossover_actors = 0
    unknown_variant_actors = 0

    for row in rows:
        variant = row.get("variant")
        if not isinstance(variant, str) or not variant:
            raise HTTPException(
                status_code=503,
                detail="ClickHouse returned invalid experiment aggregates",
            )
        if variant in seen_variants:
            raise HTTPException(
                status_code=503,
                detail="ClickHouse returned duplicate experiment arm aggregates",
            )
        seen_variants.add(variant)
        values = (
            row.get("sample_size"),
            row.get("conversions"),
            row.get("crossover_actors"),
        )
        if any(type(value) is not int for value in values):
            raise HTTPException(
                status_code=503,
                detail="ClickHouse returned invalid experiment aggregates",
            )
        sample_size, conversions, crossovers = values
        if (
            sample_size < 0
            or conversions < 0
            or conversions > sample_size
            or crossovers < 0
            or crossovers > sample_size
        ):
            raise HTTPException(
                status_code=503,
                detail="ClickHouse returned inconsistent experiment aggregates",
            )

        crossover_actors += crossovers
        if variant not in declared:
            unknown_variant_actors += sample_size
            continue
        aggregates[variant] = (sample_size, conversions)

    arms: list[ExperimentArmResult] = []
    for variant in metadata.variants:
        sample_size, conversions = aggregates.get(variant, (0, 0))
        arms.append(
            ExperimentArmResult(
                variant=variant,
                sample_size=sample_size,
                conversions=conversions,
                conversion_rate=(conversions / sample_size if sample_size else 0.0),
            )
        )
    return arms, crossover_actors, unknown_variant_actors


def _comparison(
    control: ExperimentArmResult,
    treatment: ExperimentArmResult,
    comparison_count: int,
) -> ExperimentComparison | None:
    control_rate = control.conversion_rate
    treatment_rate = treatment.conversion_rate
    difference = treatment_rate - control_rate

    pooled_rate = (
        (control.conversions + treatment.conversions)
        / (control.sample_size + treatment.sample_size)
    )
    pooled_variance = pooled_rate * (1.0 - pooled_rate) * (
        (1.0 / control.sample_size) + (1.0 / treatment.sample_size)
    )
    if pooled_variance == 0.0:
        raw_p_value = 1.0
    else:
        z_score = abs(difference) / math.sqrt(pooled_variance)
        raw_p_value = math.erfc(z_score / math.sqrt(2.0))

    adjusted_p_value = min(raw_p_value * comparison_count, 1.0)
    standard_error = math.sqrt(
        control_rate * (1.0 - control_rate) / control.sample_size
        + treatment_rate * (1.0 - treatment_rate) / treatment.sample_size
    )
    critical_value = NormalDist().inv_cdf(
        1.0 - (_ALPHA / (2.0 * comparison_count))
    )
    lower = max(-1.0, difference - critical_value * standard_error)
    upper = min(1.0, difference + critical_value * standard_error)

    statistics = (
        control_rate,
        treatment_rate,
        difference,
        lower,
        upper,
        raw_p_value,
        adjusted_p_value,
    )
    if not all(math.isfinite(value) for value in statistics):
        return None
    return ExperimentComparison(
        control_variant=control.variant,
        treatment_variant=treatment.variant,
        control_rate=control_rate,
        treatment_rate=treatment_rate,
        rate_difference=difference,
        confidence_interval=(lower, upper),
        raw_p_value=raw_p_value,
        adjusted_p_value=adjusted_p_value,
        is_significant=adjusted_p_value < _ALPHA,
    )


def _common_response(
    metadata: ConfigExperimentAnalysis,
    arms: list[ExperimentArmResult],
    crossover_actors: int,
    unknown_variant_actors: int,
) -> dict[str, Any]:
    return {
        "experiment_key": metadata.key,
        "flag_key": metadata.flag_key,
        "experiment_status": metadata.status,
        "control_variant": metadata.control_variant,
        "metric_event": metadata.metric_event,
        "start_date": metadata.start_date,
        "end_date": metadata.end_date,
        "config_version": metadata.version,
        "arms": arms,
        "crossover_actors": crossover_actors,
        "unknown_variant_actors": unknown_variant_actors,
    }


@router.get(
    "/experiment/{experiment_key}",
    response_model=ExperimentAnalysisResponse,
)
async def experiment_results(
    experiment_key: str = Path(..., pattern=_RESOURCE_KEY_PATTERN),
    *,
    request: Request,
    project_id: str | None = Query(None, min_length=1, max_length=64),
) -> ExperimentAnalysisResponse:
    """Analyze one Config-owned experiment using first-exposure attribution."""
    _reject_unknown_query_parameters(request)
    principal = request.state.principal
    pid = project_id if project_id is not None else principal.project_id
    require_project(request, pid, "query:read")
    api_key = request.headers.get("x-api-key", "")

    try:
        metadata = await fetch_experiment_analysis(pid, experiment_key, api_key)
    except ExperimentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExperimentNotAnalyzable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConfigServiceUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Authoritative experiment metadata is unavailable",
        ) from exc

    if metadata.end_date - metadata.start_date > _MAX_EXPERIMENT_WINDOW:
        raise HTTPException(
            status_code=422,
            detail="Authoritative experiment window must not exceed 90 days",
        )
    if metadata.status == "scheduled":
        arms = _empty_arms(metadata)
        return ExperimentAnalysisInsufficient(
            **_common_response(metadata, arms, 0, 0),
            reason="experiment_not_started",
            underpowered_variants=list(metadata.variants),
        )

    rows = await _get_client(request).execute(
        EXPERIMENT_ANALYSIS_QUERY,
        {
            "project_id": pid,
            "flag_key": metadata.flag_key,
            "metric_event": metadata.metric_event,
            "start_ms": _datetime64_boundary_milliseconds(metadata.start_date),
            "end_ms": _datetime64_boundary_milliseconds(metadata.end_date),
        },
    )
    arms, crossover_actors, unknown_variant_actors = _aggregate_arms(
        metadata,
        rows,
    )
    common = _common_response(
        metadata,
        arms,
        crossover_actors,
        unknown_variant_actors,
    )

    if sum(arm.sample_size for arm in arms) == 0:
        return ExperimentAnalysisInsufficient(
            **common,
            reason="no_exposures",
            underpowered_variants=list(metadata.variants),
        )

    underpowered = [
        arm.variant
        for arm in arms
        if arm.sample_size < _MIN_SAMPLE_SIZE_PER_ARM
    ]
    if underpowered:
        return ExperimentAnalysisInsufficient(
            **common,
            reason="underpowered_arms",
            underpowered_variants=underpowered,
        )

    by_variant = {arm.variant: arm for arm in arms}
    treatments = [
        variant
        for variant in metadata.variants
        if variant != metadata.control_variant
    ]
    comparisons: list[ExperimentComparison] = []
    for treatment in treatments:
        result = _comparison(
            by_variant[metadata.control_variant],
            by_variant[treatment],
            len(treatments),
        )
        if result is None:
            return ExperimentAnalysisInsufficient(
                **common,
                reason="non_finite_statistics",
                underpowered_variants=[],
            )
        comparisons.append(result)

    return ExperimentAnalysisReady(
        **common,
        comparisons=comparisons,
    )
