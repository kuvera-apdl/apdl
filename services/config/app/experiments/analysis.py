"""Strict projection of stored experiments into the analysis contract."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.models.schemas import (
    ExperimentAnalysis,
    ExperimentMetric,
    ExperimentStatisticalPlan,
    ExperimentTargetingRule,
    ExperimentVariant,
    validate_statistical_plan,
)


class ExperimentNotAnalyzableError(ValueError):
    """The authoritative experiment record cannot be analyzed safely."""


def _stored_json(raw: Any, *, field: str, expected_type: type) -> Any:
    if not isinstance(raw, str):
        raise ExperimentNotAnalyzableError(f"{field} is not canonical JSON text")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExperimentNotAnalyzableError(f"{field} is malformed JSON") from exc
    if not isinstance(value, expected_type):
        raise ExperimentNotAnalyzableError(
            f"{field} must contain a JSON {expected_type.__name__}"
        )
    return value


def build_analysis_contract(experiment: dict) -> ExperimentAnalysis:
    """Build the exact Query-facing contract from one stored experiment row."""
    if experiment.get("status") == "draft":
        raise ExperimentNotAnalyzableError("draft experiments are not analyzable")
    try:
        variant_values = _stored_json(
            experiment.get("variants_json"),
            field="variants_json",
            expected_type=list,
        )
        variants = [
            ExperimentVariant.model_validate(value) for value in variant_values
        ]
        metric_value = _stored_json(
            experiment.get("primary_metric_json"),
            field="primary_metric_json",
            expected_type=dict,
        )
        primary_metric = ExperimentMetric.model_validate(metric_value)
        targeting_values = _stored_json(
            experiment.get("targeting_rules_json"),
            field="targeting_rules_json",
            expected_type=list,
        )
        targeting_rules = [
            ExperimentTargetingRule.model_validate(value)
            for value in targeting_values
        ]
        statistical_plan = ExperimentStatisticalPlan.model_validate(
            experiment.get("statistical_plan")
        )
        validate_statistical_plan(
            status=experiment.get("status"),
            statistical_plan=statistical_plan,
            primary_metric=primary_metric,
            variant_count=len(variants),
        )
        return ExperimentAnalysis(
            key=experiment.get("key"),
            flag_key=experiment.get("flag_key"),
            status=experiment.get("status"),
            control_variant=experiment.get("default_variant"),
            variants=[variant.key for variant in variants],
            metric_event=primary_metric.event,
            metric_direction=primary_metric.direction,
            enrollment_mode="targeted" if targeting_rules else "all",
            minimum_exposure_config_version=experiment.get(
                "minimum_exposure_config_version"
            ),
            statistical_plan=statistical_plan,
            start_date=experiment.get("start_date"),
            end_date=experiment.get("end_date"),
            version=experiment.get("version"),
        )
    except ExperimentNotAnalyzableError:
        raise
    except (TypeError, ValueError, ValidationError) as exc:
        raise ExperimentNotAnalyzableError(str(exc)) from exc
