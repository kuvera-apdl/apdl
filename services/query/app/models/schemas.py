"""Pydantic request/response models for the Query Service."""

from __future__ import annotations

from datetime import date
from enum import Enum
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _coerce_project_id(value: Any) -> str:
    if value is None:
        raise ValueError("project_id is required")
    return str(value)


ProjectId = Annotated[str, BeforeValidator(_coerce_project_id)]


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


class AnalysisMethod(str, Enum):
    """Statistical analysis method for experiment evaluation."""
    frequentist = "frequentist"
    bayesian = "bayesian"
    sequential = "sequential"


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
        return self


# ---------------------------------------------------------------------------
# Event models
# ---------------------------------------------------------------------------

def _is_filter_scalar(value: Any) -> bool:
    return isinstance(value, str | int | float | bool) and value is not None


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
            if not all(_is_filter_scalar(item) for item in self.value):
                raise ValueError("list filter values must be strings, numbers, or booleans")

            value_kinds = {_filter_value_kind(item) for item in self.value}
            if value_kinds - {"number"} and len(value_kinds) > 1:
                raise ValueError("list filter values must use a single comparable type")
            return self

        if self.operator == EventFilterOperator.contains:
            if not isinstance(self.value, str) or not self.value:
                raise ValueError("contains requires a non-empty string value")
            return self

        if self.operator in {
            EventFilterOperator.gt,
            EventFilterOperator.gte,
            EventFilterOperator.lt,
            EventFilterOperator.lte,
        }:
            if isinstance(self.value, bool) or not isinstance(self.value, int | float):
                raise ValueError(f"{self.operator.value} requires a numeric value")
            return self

        if not _is_filter_scalar(self.value):
            raise ValueError(f"{self.operator.value} requires a scalar value")
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


class BreakdownResponse(BaseModel):
    selector: str
    property: str
    results: list[dict[str, Any]]


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
    period: Literal["day", "week"] = "day"


class RetentionCohort(BaseModel):
    cohort_date: str
    size: int
    retention: list[float]


class RetentionResponse(BaseModel):
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
# Experiment models
# ---------------------------------------------------------------------------

class ExperimentResultsRequest(BaseModel):
    experiment_id: str
    metric: str
    method: AnalysisMethod = AnalysisMethod.frequentist


class VariantResult(BaseModel):
    variant: str
    users: int
    mean: float
    stddev: float
    total: float


class ExperimentResult(BaseModel):
    experiment_id: str
    metric: str
    method: str
    variants: list[VariantResult]
    effect_size: float | None = None
    confidence_interval: tuple[float, float] | None = None
    p_value: float | None = None
    is_significant: bool
    recommendation: str
