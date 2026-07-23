"""Strict contracts for experiment-design LLM output and safety review."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


_RESOURCE_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
EXPERIMENT_BUCKET_BY_VALUES = frozenset({"anonymous_id", "user_id"})
ExperimentBucketBy = Literal["anonymous_id", "user_id"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExperimentVariant(_StrictModel):
    key: str = Field(min_length=1, max_length=128)
    weight: int = Field(gt=0)
    description: str = Field(min_length=1)


class FlagVariant(_StrictModel):
    """The flag projection intentionally contains only assignment fields."""

    key: str = Field(min_length=1, max_length=128)
    weight: int = Field(gt=0)


class PrimaryMetric(_StrictModel):
    event: str = Field(min_length=1)
    type: Literal["conversion"]
    direction: Literal["increase", "decrease"]


class StatisticalPlan(_StrictModel):
    protocol: Literal["fixed_horizon_fisher_newcombe_cc_plan_v1"]
    baseline_conversion_rate: int | float = Field(ge=0, le=1)
    minimum_detectable_effect: int | float = Field(gt=0, le=1)
    significance_level: int | float = Field(gt=0, le=0.5)
    nominal_power: int | float = Field(gt=0.5, le=0.9999)
    required_sample_size_per_arm: int = Field(ge=2, le=10_000_000)
    data_settlement_seconds: int = Field(ge=1, le=86_400)


TargetValue = str | int | float | bool | list[str | int | float | bool]
_VALUELESS_OPERATORS = {"exists", "not_exists"}


class TargetingCondition(_StrictModel):
    attribute: str = Field(min_length=1)
    operator: Literal[
        "equals",
        "not_equals",
        "gt",
        "gte",
        "lt",
        "lte",
        "contains",
        "not_contains",
        "starts_with",
        "ends_with",
        "in",
        "not_in",
        "exists",
        "not_exists",
    ]
    value: TargetValue | None = None

    @model_validator(mode="after")
    def validate_value_presence(self) -> "TargetingCondition":
        supplied = "value" in self.model_fields_set
        if self.operator in _VALUELESS_OPERATORS:
            if supplied:
                raise ValueError(f"{self.operator} must omit value")
        elif not supplied or self.value is None:
            raise ValueError(f"{self.operator} requires a non-null value")
        return self


class Targeting(_StrictModel):
    conditions: list[TargetingCondition] = Field(max_length=50)


class Rollout(_StrictModel):
    percentage: int | float = Field(ge=0, le=100)
    bucket_by: ExperimentBucketBy


class Fallthrough(_StrictModel):
    rollout: Rollout


class FlagRule(_StrictModel):
    """A typed item for the deliberately empty rules list."""

    rollout: Rollout


class ExperimentFlagConfig(_StrictModel):
    key: str = Field(pattern=_RESOURCE_KEY_PATTERN)
    name: str = Field(min_length=1)
    default_variant: str = Field(min_length=1, max_length=128)
    variants: list[FlagVariant] = Field(min_length=2, max_length=10)
    # Experiment targeting has one canonical home: top-level ``targeting``.
    rules: list[FlagRule] = Field(max_length=0)
    fallthrough: Fallthrough
    evaluation_mode: Literal["client"]
    auto_disable: Literal[False]


class ExperimentDesign(_StrictModel):
    experiment_id: str = Field(pattern=_RESOURCE_KEY_PATTERN)
    source_insight: str = Field(min_length=1)
    hypothesis: str = Field(min_length=10)
    description: str = Field(min_length=1)
    treatment_spec: str
    variants: list[ExperimentVariant] = Field(min_length=2, max_length=10)
    primary_metric: PrimaryMetric
    targeting: Targeting
    estimated_duration_days: int = Field(ge=1, le=90)
    statistical_plan: StatisticalPlan
    flag_config: ExperimentFlagConfig

    @model_validator(mode="after")
    def validate_variant_contract(self) -> "ExperimentDesign":
        experiment_keys = [variant.key for variant in self.variants]
        flag_keys = [variant.key for variant in self.flag_config.variants]
        if len(set(experiment_keys)) != len(experiment_keys):
            raise ValueError("variants must contain unique keys")
        if len(set(flag_keys)) != len(flag_keys):
            raise ValueError("flag_config.variants must contain unique keys")
        if experiment_keys != flag_keys:
            raise ValueError(
                "flag_config.variants must project the top-level variant keys in order"
            )
        if self.flag_config.default_variant not in experiment_keys:
            raise ValueError("flag_config.default_variant must match a variant key")

        # Relative weights may use different scales (50/50 and 1/1), but they
        # must describe the identical assignment distribution.
        top_total = sum(variant.weight for variant in self.variants)
        flag_total = sum(variant.weight for variant in self.flag_config.variants)
        if any(
            top.weight * flag_total != flag.weight * top_total
            for top, flag in zip(self.variants, self.flag_config.variants, strict=True)
        ):
            raise ValueError(
                "flag_config variant weights must match the top-level distribution"
            )
        return self


class ExperimentSafetyReview(_StrictModel):
    approved: bool
    concerns: list[str]
    risk_level: Literal["low", "medium", "high"]
    recommendations: list[str]
