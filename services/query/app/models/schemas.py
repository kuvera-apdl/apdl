"""Pydantic request/response models for the Query Service."""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


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


# ---------------------------------------------------------------------------
# Shared base with date-range validation
# ---------------------------------------------------------------------------

class DateRangeRequest(BaseModel):
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

class EventCountRequest(DateRangeRequest):
    project_id: int
    event_names: list[str] | None = None


class EventCountResponse(BaseModel):
    results: list[dict[str, Any]]
    total_events: int
    total_users: int


class TimeseriesRequest(DateRangeRequest):
    project_id: int
    event_name: str
    interval: TimeInterval = TimeInterval.day


class TimeseriesResponse(BaseModel):
    buckets: list[dict[str, Any]]


class BreakdownRequest(DateRangeRequest):
    project_id: int
    event_name: str
    property: str
    limit: int = Field(default=20, ge=1, le=100)


class BreakdownResponse(BaseModel):
    results: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Funnel models
# ---------------------------------------------------------------------------

class FunnelRequest(DateRangeRequest):
    project_id: int
    steps: list[str] = Field(..., min_length=2)
    window_days: int = Field(default=7, ge=1, le=90)


class FunnelStep(BaseModel):
    step: int
    event_name: str
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
    project_id: int
    cohort_event: str
    return_event: str
    period: Literal["day", "week"] = "day"


class RetentionCohort(BaseModel):
    cohort_date: str
    size: int
    retention: list[float]


class RetentionResponse(BaseModel):
    cohorts: list[RetentionCohort]


# ---------------------------------------------------------------------------
# Cohort comparison models
# ---------------------------------------------------------------------------

class CohortRequest(DateRangeRequest):
    project_id: int
    cohort_property: str
    metric_event: str


class CohortResponse(BaseModel):
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
