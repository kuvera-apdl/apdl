"""Experiment results endpoint — statistical analysis of A/B experiments."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Request

from app.clickhouse.client import ClickHouseClient
from app.clickhouse.queries import EXPERIMENT_EXPOSURES_QUERY, EXPERIMENT_METRICS_QUERY
from app.models.schemas import AnalysisMethod, ExperimentResult, VariantResult
from app.models.statistics import ExperimentAnalyzer

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/query", tags=["experiments"])

analyzer = ExperimentAnalyzer()

DEFAULT_PROJECT_ID = os.getenv("DEFAULT_PROJECT_ID", "default")


def _get_client(request: Request) -> ClickHouseClient:
    return request.app.state.ch_client


@router.get("/experiment/{experiment_id}", response_model=ExperimentResult)
async def experiment_results(
    experiment_id: str,
    request: Request,
    metric: str = Query(..., description="The conversion/metric event name to evaluate"),
    flag_key: str = Query(
        ...,
        min_length=1,
        description="Feature flag key that produced canonical variant exposures",
    ),
    method: AnalysisMethod = Query(
        AnalysisMethod.frequentist,
        description="Statistical method: frequentist, bayesian, or sequential",
    ),
    project_id: str | None = Query(
        None,
        description="Project ID (defaults to env DEFAULT_PROJECT_ID)",
    ),
) -> ExperimentResult:
    """Retrieve and statistically analyse experiment results.

    Fetches feature-flag variant assignments and per-user metric values from
    ClickHouse, then runs the selected statistical test.
    """
    client = _get_client(request)
    pid = project_id if project_id is not None else DEFAULT_PROJECT_ID

    params: dict[str, Any] = {
        "project_id": pid,
        "flag_key": flag_key,
        "metric": metric,
    }

    # ---- Fetch per-user metric values grouped by variant ----
    metric_rows = await client.execute(EXPERIMENT_METRICS_QUERY, params)

    if not metric_rows:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No data found for experiment '{experiment_id}' "
                f"and flag '{flag_key}' with metric '{metric}'."
            ),
        )

    # Also fetch exposures to include users with zero conversions
    exposure_rows = await client.execute(EXPERIMENT_EXPOSURES_QUERY, params)

    # Build per-variant user sets from exposures
    exposed_users: dict[str, set[str]] = defaultdict(set)
    for row in exposure_rows:
        variant = row.get("variant", "unknown")
        user_id = row.get("user_id", "")
        exposed_users[variant].add(user_id)

    # Build per-variant metric arrays (include zeros for exposed but non-converting users)
    user_metrics: dict[str, dict[str, float]] = defaultdict(dict)
    for row in metric_rows:
        variant = row.get("variant", "unknown")
        user_id = row.get("user_id", "")
        user_metrics[variant][user_id] = float(row.get("metric_value", 0))

    variant_arrays: dict[str, np.ndarray] = {}
    for variant, users in exposed_users.items():
        values = [user_metrics.get(variant, {}).get(uid, 0.0) for uid in users]
        variant_arrays[variant] = np.array(values, dtype=np.float64)

    if len(variant_arrays) < 2:
        raise HTTPException(
            status_code=400,
            detail="Experiment must have at least two variants for statistical analysis.",
        )

    # ---- Build variant summaries ----
    variant_results: list[VariantResult] = []
    for variant_name, arr in sorted(variant_arrays.items()):
        variant_results.append(
            VariantResult(
                variant=variant_name,
                users=len(arr),
                mean=float(np.mean(arr)) if len(arr) > 0 else 0.0,
                stddev=float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0,
                total=float(np.sum(arr)),
            )
        )

    # ---- Identify control and treatment ----
    sorted_variants = sorted(variant_arrays.keys())
    control_key = "control" if "control" in sorted_variants else sorted_variants[0]
    treatment_keys = [k for k in sorted_variants if k != control_key]

    if not treatment_keys:
        raise HTTPException(status_code=400, detail="No treatment variant found.")

    # For multi-variant experiments, compare each treatment against control.
    # Return the result for the first treatment variant (most common case).
    treatment_key = treatment_keys[0]

    control_arr = variant_arrays[control_key]
    treatment_arr = variant_arrays[treatment_key]

    # ---- Run statistical test ----
    if method == AnalysisMethod.bayesian:
        # For Bayesian test, convert metric to binary (>0 means conversion)
        control_binary = (control_arr > 0).astype(np.float64)
        treatment_binary = (treatment_arr > 0).astype(np.float64)
        result = analyzer.bayesian_test(control_binary, treatment_binary)
    elif method == AnalysisMethod.sequential:
        result = analyzer.sequential_test(control_arr, treatment_arr)
    else:
        result = analyzer.frequentist_test(control_arr, treatment_arr)

    # Extract common fields
    ci = result.get("confidence_interval")
    p_value = result.get("p_value") or result.get("always_valid_p_value")

    return ExperimentResult(
        experiment_id=experiment_id,
        flag_key=flag_key,
        metric=metric,
        method=method.value,
        variants=variant_results,
        effect_size=result.get("effect_size"),
        confidence_interval=ci,
        p_value=p_value,
        is_significant=result["is_significant"],
        recommendation=result["recommendation"],
    )
