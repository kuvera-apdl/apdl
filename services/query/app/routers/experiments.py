"""Authoritative, exposure-led experiment analysis."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from scipy.stats import fisher_exact

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
    ExperimentAnalysisDecisionSnapshot,
    ExperimentAnalysisNonFinal,
    ExperimentAnalysisResponse,
    ExperimentArmResult,
    ExperimentComparison,
    ProjectId,
)

router = APIRouter(prefix="/v1/query", tags=["experiments"])

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
) -> tuple[list[ExperimentArmResult], int, int, int]:
    declared = set(metadata.variants)
    aggregates: dict[str, tuple[int, int]] = {}
    seen_variants: set[str] = set()
    crossover_actors = 0
    unknown_variant_actors = 0
    identity_conflict_actors: int | None = None

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
        row_identity_conflicts = row.get("identity_conflict_actors")
        if type(row_identity_conflicts) is not int or row_identity_conflicts < 0:
            raise HTTPException(
                status_code=503,
                detail="ClickHouse returned invalid identity-conflict aggregates",
            )
        if identity_conflict_actors is None:
            identity_conflict_actors = row_identity_conflicts
        elif identity_conflict_actors != row_identity_conflicts:
            raise HTTPException(
                status_code=503,
                detail="ClickHouse returned inconsistent identity-conflict aggregates",
            )
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
    return (
        arms,
        crossover_actors,
        unknown_variant_actors,
        identity_conflict_actors or 0,
    )


def _comparison(
    control: ExperimentArmResult,
    treatment: ExperimentArmResult,
    comparison_count: int,
    significance_level: float,
) -> ExperimentComparison | None:
    control_rate = control.conversion_rate
    treatment_rate = treatment.conversion_rate
    difference = treatment_rate - control_rate

    raw_p_value = float(
        fisher_exact(
            [
                [
                    treatment.conversions,
                    treatment.sample_size - treatment.conversions,
                ],
                [
                    control.conversions,
                    control.sample_size - control.conversions,
                ],
            ],
            alternative="two-sided",
        ).pvalue
    )

    adjusted_p_value = min(raw_p_value * comparison_count, 1.0)
    critical_value = NormalDist().inv_cdf(
        1.0 - (significance_level / (2.0 * comparison_count))
    )
    control_lower, control_upper = _wilson_interval(
        control.conversions,
        control.sample_size,
        critical_value,
    )
    treatment_lower, treatment_upper = _wilson_interval(
        treatment.conversions,
        treatment.sample_size,
        critical_value,
    )
    lower = max(
        -1.0,
        difference
        - math.sqrt(
            (treatment_rate - treatment_lower) ** 2
            + (control_upper - control_rate) ** 2
        ),
    )
    upper = min(
        1.0,
        difference
        + math.sqrt(
            (treatment_upper - treatment_rate) ** 2
            + (control_rate - control_lower) ** 2
        ),
    )

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
        is_statistically_significant=adjusted_p_value < significance_level,
    )


def _wilson_interval(
    successes: int,
    sample_size: int,
    critical_value: float,
) -> tuple[float, float]:
    """Wilson score interval for one binomial proportion."""
    proportion = successes / sample_size
    z_squared = critical_value**2
    denominator = 1.0 + z_squared / sample_size
    centre = (proportion + z_squared / (2.0 * sample_size)) / denominator
    half_width = (
        critical_value
        * math.sqrt(
            proportion * (1.0 - proportion) / sample_size
            + z_squared / (4.0 * sample_size**2)
        )
        / denominator
    )
    return max(0.0, centre - half_width), min(1.0, centre + half_width)


def _common_response(
    metadata: ConfigExperimentAnalysis,
    arms: list[ExperimentArmResult],
    crossover_actors: int,
    unknown_variant_actors: int,
    identity_conflict_actors: int = 0,
) -> dict[str, Any]:
    return {
        "experiment_key": metadata.key,
        "flag_key": metadata.flag_key,
        "experiment_status": metadata.status,
        "control_variant": metadata.control_variant,
        "metric_event": metadata.metric_event,
        "metric_direction": metadata.metric_direction,
        "statistical_plan": metadata.statistical_plan.model_dump(),
        "start_date": metadata.start_date,
        "end_date": metadata.end_date,
        "config_version": metadata.version,
        "arms": arms,
        "crossover_actors": crossover_actors,
        "unknown_variant_actors": unknown_variant_actors,
        "identity_conflict_actors": identity_conflict_actors,
        "identity_quality": (
            "degraded" if identity_conflict_actors else "unambiguous"
        ),
    }


@router.get(
    "/experiment/{experiment_key}",
    response_model=ExperimentAnalysisResponse,
)
async def experiment_results(
    experiment_key: str = Path(..., pattern=_RESOURCE_KEY_PATTERN),
    *,
    request: Request,
    project_id: ProjectId | None = Query(None),
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
    current_time = datetime.now(timezone.utc)
    if (
        metadata.status == "completed"
        and metadata.end_date > current_time
    ):
        arms = _empty_arms(metadata)
        return ExperimentAnalysisNonFinal(
            **_common_response(metadata, arms, 0, 0),
            reason="experiment_window_open",
            underpowered_variants=list(metadata.variants),
        )
    settlement_end = metadata.end_date + timedelta(
        seconds=metadata.statistical_plan.data_settlement_seconds
    )
    if metadata.status == "completed" and current_time < settlement_end:
        arms = _empty_arms(metadata)
        return ExperimentAnalysisNonFinal(
            **_common_response(metadata, arms, 0, 0),
            reason="awaiting_data_settlement",
            underpowered_variants=list(metadata.variants),
        )
    if metadata.status == "scheduled":
        arms = _empty_arms(metadata)
        return ExperimentAnalysisNonFinal(
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
    (
        arms,
        crossover_actors,
        unknown_variant_actors,
        identity_conflict_actors,
    ) = _aggregate_arms(
        metadata,
        rows,
    )
    common = _common_response(
        metadata,
        arms,
        crossover_actors,
        unknown_variant_actors,
        identity_conflict_actors,
    )

    required_sample_size = metadata.statistical_plan.required_sample_size_per_arm
    underpowered = [
        arm.variant
        for arm in arms
        if arm.sample_size < required_sample_size
    ]
    if identity_conflict_actors:
        return ExperimentAnalysisNonFinal(
            **common,
            reason="identity_alias_conflicts",
            underpowered_variants=underpowered,
        )
    if metadata.status == "running":
        return ExperimentAnalysisNonFinal(
            **common,
            reason="experiment_running",
            underpowered_variants=underpowered,
        )
    if metadata.status == "stopped":
        return ExperimentAnalysisNonFinal(
            **common,
            reason="experiment_stopped",
            underpowered_variants=underpowered,
        )

    if sum(arm.sample_size for arm in arms) == 0:
        return ExperimentAnalysisNonFinal(
            **common,
            reason="no_exposures",
            underpowered_variants=list(metadata.variants),
        )

    if underpowered:
        return ExperimentAnalysisNonFinal(
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
            metadata.statistical_plan.significance_level,
        )
        if result is None:
            return ExperimentAnalysisNonFinal(
                **common,
                reason="non_finite_statistics",
                underpowered_variants=[],
            )
        comparisons.append(result)

    return ExperimentAnalysisDecisionSnapshot(
        **common,
        comparisons=comparisons,
    )
