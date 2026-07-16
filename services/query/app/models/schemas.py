"""Pydantic request/response models for the Query Service."""

from __future__ import annotations

from datetime import date
from enum import Enum
import math
import re
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)


def _coerce_project_id(value: Any) -> str:
    if value is None:
        raise ValueError("project_id is required")
    return str(value)


ProjectId = Annotated[str, BeforeValidator(_coerce_project_id)]


class StrictModel(BaseModel):
    """Base model for strict public request contracts."""

    model_config = ConfigDict(extra="forbid")
    
_PROPERTY_NAME_RE = re.compile(r"^[A-Za-z0-9_$][A-Za-z0-9_.$:-]{0,127}$")

def _validate_property_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("property must be a string")
    if not _PROPERTY_NAME_RE.fullmatch(value):
        raise ValueError(
            "property may contain only letters, numbers, _, -, :, ., and $, "
            "and must start with a letter, number, _, or $"
        )
    return value


PropertyName = Annotated[str, BeforeValidator(_validate_property_name)]


# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------

class TimeInterval(str, Enum):
    """Supported time-bucket intervals for timeseries queries."""
    hour = "1 HOUR"
    day = "1 DAY"
    week = "1 WEEK"
    month = "1 MONTH"


class GuardrailMetric(str, Enum):
    """Supported feature-flag guardrail metrics."""
    frontend_error_rate = "frontend_error_rate"
    frontend_error_count = "frontend_error_count"


class GuardrailThreshold(str, Enum):
    """Supported feature-flag guardrail thresholds."""
    two_x_baseline = "2x_baseline"
    at_least_one = "at_least_one"
class EventFilterOperator(str, Enum):
    """Supported property filter operators for event selectors."""
    eq = "eq"
    neq = "neq"
    in_ = "in"
    not_in = "not_in"
    exists = "exists"
    not_exists = "not_exists"
    contains = "contains"
    gt = "gt"
    gte = "gte"
    lt = "lt"
    lte = "lte"


# ---------------------------------------------------------------------------
# Shared base with date-range validation
# ---------------------------------------------------------------------------

class DateRangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def check_date_range(self) -> "DateRangeRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if (self.end_date - self.start_date).days > 90:
            raise ValueError("query date range must not exceed 90 days")
        return self


# ---------------------------------------------------------------------------
# Event models
# ---------------------------------------------------------------------------

MAX_FILTER_MEMBERSHIP_VALUES = 100
MAX_FILTER_STRING_LENGTH = 1_024


def _is_filter_scalar(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) <= MAX_FILTER_STRING_LENGTH
    if isinstance(value, bool):
        return True
    if isinstance(value, int | float):
        try:
            return math.isfinite(float(value))
        except OverflowError:
            return False
    return False


def _filter_value_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    raise ValueError("filter value must be a string, number, boolean, or list of those values")


class EventPropertyFilter(BaseModel):
    """A single property predicate inside an event selector."""

    model_config = ConfigDict(extra="forbid")

    property: PropertyName
    operator: EventFilterOperator
    value: Any = None

    @model_validator(mode="after")
    def check_operator_value(self) -> "EventPropertyFilter":
        if self.operator in {EventFilterOperator.exists, EventFilterOperator.not_exists}:
            if "value" in self.model_fields_set:
                raise ValueError(f"{self.operator.value} does not accept a value")
            return self

        if self.value is None:
            raise ValueError(f"{self.operator.value} requires a value")

        if self.operator in {EventFilterOperator.in_, EventFilterOperator.not_in}:
            if not isinstance(self.value, list) or len(self.value) == 0:
                raise ValueError(f"{self.operator.value} requires a non-empty list value")
            if len(self.value) > MAX_FILTER_MEMBERSHIP_VALUES:
                raise ValueError(
                    f"{self.operator.value} accepts at most "
                    f"{MAX_FILTER_MEMBERSHIP_VALUES} values"
                )
            if not all(_is_filter_scalar(item) for item in self.value):
                raise ValueError(
                    "list filter values must be finite numbers, booleans, or strings "
                    f"of at most {MAX_FILTER_STRING_LENGTH} characters"
                )

            value_kinds = {_filter_value_kind(item) for item in self.value}
            if value_kinds - {"number"} and len(value_kinds) > 1:
                raise ValueError("list filter values must use a single comparable type")
            return self

        if self.operator == EventFilterOperator.contains:
            if not isinstance(self.value, str) or not self.value:
                raise ValueError("contains requires a non-empty string value")
            if len(self.value) > MAX_FILTER_STRING_LENGTH:
                raise ValueError(
                    f"contains accepts at most {MAX_FILTER_STRING_LENGTH} characters"
                )
            return self

        if self.operator in {
            EventFilterOperator.gt,
            EventFilterOperator.gte,
            EventFilterOperator.lt,
            EventFilterOperator.lte,
        }:
            if (
                isinstance(self.value, bool)
                or not isinstance(self.value, int | float)
                or not _is_filter_scalar(self.value)
            ):
                raise ValueError(
                    f"{self.operator.value} requires a finite numeric value"
                )
            return self

        if not _is_filter_scalar(self.value):
            raise ValueError(
                f"{self.operator.value} requires a bounded finite scalar value"
            )
        return self


class EventSelector(BaseModel):
    """Reusable event selector shared by Query Service analytics endpoints."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "event_name": "$click",
                    "filters": [
                        {
                            "property": "href",
                            "operator": "eq",
                            "value": "http://localhost:3000/catalog",
                        }
                    ],
                }
            ]
        },
    )

    event_name: str = Field(..., min_length=1, max_length=256)
    filters: list[EventPropertyFilter] = Field(default_factory=list, max_length=25)


class EventCountRequest(DateRangeRequest):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "project_id": "apiasport",
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-31",
                    "selectors": [
                        {
                            "event_name": "$click",
                            "filters": [
                                {"property": "href", "operator": "eq", "value": "/pricing"}
                            ],
                        }
                    ],
                }
            ]
        },
    )

    project_id: ProjectId
    selectors: list[EventSelector] = Field(..., min_length=1, max_length=20)


class EventCountResponse(BaseModel):
    results: list[dict[str, Any]]
    total_events: int
    total_users: int


class EventCatalogRequest(DateRangeRequest):
    """Discover which event names exist for a project in a date range."""

    project_id: ProjectId
    limit: int = Field(default=100, ge=1, le=1000)


class EventCatalogResponse(BaseModel):
    events: list[dict[str, Any]]


class TimeseriesRequest(DateRangeRequest):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "project_id": "apiasport",
                    "selector": {
                        "event_name": "$click",
                        "filters": [{"property": "text", "operator": "eq", "value": "Get started"}],
                    },
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-31",
                    "interval": "1 DAY",
                }
            ]
        },
    )

    project_id: ProjectId
    selector: EventSelector
    interval: TimeInterval = TimeInterval.day


class TimeseriesResponse(BaseModel):
    selector: str
    buckets: list[dict[str, Any]]


class BreakdownRequest(DateRangeRequest):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "project_id": "apiasport",
                    "selector": {
                        "event_name": "$click",
                        "filters": [{"property": "page.path", "operator": "eq", "value": "/pricing"}],
                    },
                    "property": "href",
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-31",
                    "limit": 20,
                }
            ]
        },
    )

    project_id: ProjectId
    selector: EventSelector
    property: PropertyName
    limit: int = Field(default=20, ge=1, le=100)


class BreakdownResult(StrictModel):
    """One canonical typed scalar bucket returned by a breakdown query."""

    model_config = ConfigDict(extra="forbid", strict=True)

    selector: str
    property_type: Literal["string", "integer", "float", "boolean"]
    property_value: str
    event_count: int = Field(..., ge=0)
    unique_users: int = Field(..., ge=0)


class BreakdownResponse(StrictModel):
    selector: str
    property: str
    results: list[BreakdownResult]


# ---------------------------------------------------------------------------
# Funnel models
# ---------------------------------------------------------------------------

class FunnelRequest(DateRangeRequest):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "project_id": "apiasport",
                    "steps": [
                        {
                            "event_name": "$pageview",
                            "filters": [{"property": "path", "operator": "eq", "value": "/catalog"}],
                        },
                        {
                            "event_name": "$click",
                            "filters": [{"property": "href", "operator": "eq", "value": "/checkout"}],
                        },
                    ],
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-31",
                    "window_days": 7,
                }
            ]
        },
    )

    project_id: ProjectId
    steps: list[EventSelector] = Field(..., min_length=2, max_length=20)
    window_days: int = Field(default=7, ge=1, le=90)


class FunnelStep(BaseModel):
    step: int
    event_name: str
    selector: str
    count: int
    conversion_rate: float
    overall_rate: float


class FunnelResponse(BaseModel):
    steps: list[FunnelStep]
    overall_conversion: float


# ---------------------------------------------------------------------------
# Retention models
# ---------------------------------------------------------------------------

class RetentionRequest(DateRangeRequest):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "project_id": "apiasport",
                    "cohort_selector": {
                        "event_name": "$pageview",
                        "filters": [{"property": "path", "operator": "eq", "value": "/pricing"}],
                    },
                    "return_selector": {
                        "event_name": "$click",
                        "filters": [{"property": "href", "operator": "eq", "value": "/signup"}],
                    },
                    "cohort_mode": "first_match_in_window",
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-31",
                    "period": "day",
                }
            ]
        },
    )

    project_id: ProjectId
    cohort_selector: EventSelector
    return_selector: EventSelector
    cohort_mode: Literal["first_match_in_window"]
    period: Literal["day", "week"] = "day"


class RetentionCohort(BaseModel):
    cohort_date: str
    size: int
    retention: list[float]


class RetentionResponse(BaseModel):
    cohort_mode: Literal["first_match_in_window"]
    cohort_selector: str
    return_selector: str
    cohorts: list[RetentionCohort]


# ---------------------------------------------------------------------------
# Cohort comparison models
# ---------------------------------------------------------------------------

class CohortRequest(DateRangeRequest):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "project_id": "apiasport",
                    "cohort_property": "plan",
                    "metric_selector": {
                        "event_name": "$click",
                        "filters": [{"property": "href", "operator": "eq", "value": "/checkout"}],
                    },
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-31",
                }
            ]
        },
    )

    project_id: ProjectId
    cohort_property: PropertyName
    metric_selector: EventSelector


class CohortResponse(BaseModel):
    metric_selector: str
    cohort_property: str
    cohorts: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Feature flag guardrail models
# ---------------------------------------------------------------------------

class GuardrailConfig(StrictModel):
    metric: GuardrailMetric
    threshold: GuardrailThreshold
    scope: str = Field(default="", max_length=512)
    minimum_exposures: int = Field(default=0, ge=0)
    window_minutes: int = Field(default=10, ge=1, le=129_600)

    @model_validator(mode="after")
    def check_guardrail_shape(self) -> "GuardrailConfig":
        if self.metric == GuardrailMetric.frontend_error_rate:
            if self.threshold != GuardrailThreshold.two_x_baseline:
                raise ValueError("frontend_error_rate requires threshold '2x_baseline'")
        if self.metric == GuardrailMetric.frontend_error_count:
            if self.threshold != GuardrailThreshold.at_least_one:
                raise ValueError("frontend_error_count requires threshold 'at_least_one'")
        if self.scope and not self.scope.startswith("page:"):
            raise ValueError("guardrail scope must be empty or start with 'page:'")
        return self


class GuardrailVariantConfig(StrictModel):
    key: str = Field(..., min_length=1, max_length=128)
    weight: int = Field(..., ge=0, strict=True)


class GuardrailVariantContext(StrictModel):
    default_variant: str = Field(..., min_length=1, max_length=128)
    variants: list[GuardrailVariantConfig] = Field(
        ...,
        min_length=1,
        max_length=10,
    )

    @model_validator(mode="after")
    def check_variant_context(self) -> "GuardrailVariantContext":
        keys: set[str] = set()
        total_weight = 0
        for variant in self.variants:
            if variant.key in keys:
                raise ValueError("variants must contain unique keys")
            keys.add(variant.key)
            total_weight += variant.weight

        if total_weight <= 0:
            raise ValueError("variant weights must contain at least one positive weight")
        if self.default_variant not in keys:
            raise ValueError("default_variant must match a variant key")
        return self


class GuardrailEvaluateRequest(GuardrailVariantContext):
    project_id: ProjectId
    flag_key: str = Field(..., min_length=1, max_length=128)
    guardrail: GuardrailConfig


class GuardrailEvaluateResponse(BaseModel):
    flag_key: str
    metric: str
    threshold: str
    scope: str
    window_minutes: int
    tripped: bool
    evidence: dict[str, Any]


# ---------------------------------------------------------------------------
# Experiment-analysis models
# ---------------------------------------------------------------------------

class _FiniteExperimentModel(StrictModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ExperimentArmResult(_FiniteExperimentModel):
    variant: str
    sample_size: int = Field(..., ge=0)
    conversions: int = Field(..., ge=0)
    conversion_rate: float = Field(..., ge=0.0, le=1.0)


class ExperimentStatisticalPlan(_FiniteExperimentModel):
    protocol: Literal["fixed_horizon_fisher_newcombe_cc_plan_v1"]
    baseline_conversion_rate: float = Field(..., ge=0.0, le=1.0, strict=True)
    minimum_detectable_effect: float = Field(..., ge=1e-6, le=1.0, strict=True)
    significance_level: float = Field(..., ge=1e-6, le=0.5, strict=True)
    nominal_power: float = Field(..., gt=0.5, le=0.9999, strict=True)
    required_sample_size_per_arm: int = Field(
        ..., ge=2, le=10_000_000, strict=True
    )
    data_settlement_seconds: int = Field(..., ge=1, le=86_400, strict=True)


class ExperimentComparison(_FiniteExperimentModel):
    control_variant: str
    treatment_variant: str
    control_rate: float = Field(..., ge=0.0, le=1.0)
    treatment_rate: float = Field(..., ge=0.0, le=1.0)
    rate_difference: float = Field(..., ge=-1.0, le=1.0)
    confidence_interval: tuple[float, float]
    raw_p_value: float = Field(..., ge=0.0, le=1.0)
    adjusted_p_value: float = Field(..., ge=0.0, le=1.0)
    is_statistically_significant: bool


class _ExperimentAnalysisBase(_FiniteExperimentModel):
    experiment_key: str
    flag_key: str
    experiment_status: Literal["scheduled", "running", "completed", "stopped"]
    control_variant: str
    metric_event: str
    metric_direction: Literal["increase", "decrease"]
    statistical_plan: ExperimentStatisticalPlan
    start_date: AwareDatetime
    end_date: AwareDatetime
    config_version: int = Field(..., ge=1)
    arms: list[ExperimentArmResult]
    crossover_actors: int = Field(..., ge=0)
    unknown_variant_actors: int = Field(..., ge=0)
    identity_conflict_actors: int = Field(..., ge=0)
    identity_quality: Literal["degraded", "unambiguous"]
    data_completeness: Literal["not_verified"] = "not_verified"
    deployment_readiness: Literal["not_assessed"] = "not_assessed"


class ExperimentAnalysisDecisionSnapshot(_ExperimentAnalysisBase):
    analysis_status: Literal["decision_snapshot"] = "decision_snapshot"
    inference_method: Literal["fisher_exact_two_sided"] = "fisher_exact_two_sided"
    interval_method: Literal["newcombe_wilson"] = "newcombe_wilson"
    correction: Literal["bonferroni"] = "bonferroni"
    comparisons: list[ExperimentComparison]


class ExperimentAnalysisNonFinal(_ExperimentAnalysisBase):
    analysis_status: Literal["non_final"] = "non_final"
    reason: Literal[
        "experiment_not_started",
        "experiment_window_open",
        "awaiting_data_settlement",
        "experiment_running",
        "experiment_stopped",
        "no_exposures",
        "underpowered_arms",
        "non_finite_statistics",
        "identity_alias_conflicts",
    ]
    underpowered_variants: list[str]


ExperimentAnalysisResponse = Annotated[
    ExperimentAnalysisDecisionSnapshot | ExperimentAnalysisNonFinal,
    Field(discriminator="analysis_status"),
]
