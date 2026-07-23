"""Strict client for authoritative experiment-analysis metadata from Config."""

from __future__ import annotations

import os
import re
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

CONFIG_SERVICE_URL = os.getenv("CONFIG_SERVICE_URL", "http://localhost:8081")
_TIMEOUT_SECONDS = 5.0
_API_KEY_PATTERN = re.compile(
    r"^proj_(?P<project_id>[A-Za-z0-9]{1,64})_[A-Za-z0-9]{16,128}$"
)
_RESOURCE_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class ConfigExperimentStatisticalPlan(BaseModel):
    """The immutable fixed-horizon plan Config owns for Query."""

    model_config = ConfigDict(extra="forbid")

    protocol: Literal["fixed_horizon_fisher_newcombe_cc_plan_v1"]
    baseline_conversion_rate: float = Field(
        ..., ge=0.0, le=1.0, strict=True, allow_inf_nan=False
    )
    minimum_detectable_effect: float = Field(
        ..., ge=1e-6, le=1.0, strict=True, allow_inf_nan=False
    )
    significance_level: float = Field(
        ..., ge=1e-6, le=0.5, strict=True, allow_inf_nan=False
    )
    nominal_power: float = Field(
        ..., gt=0.5, le=0.9999, strict=True, allow_inf_nan=False
    )
    required_sample_size_per_arm: int = Field(
        ..., ge=2, le=10_000_000, strict=True
    )
    data_settlement_seconds: int = Field(..., ge=1, le=86_400, strict=True)


class ConfigExperimentAnalysis(BaseModel):
    """The one Config contract Query accepts for experiment analysis."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., pattern=_RESOURCE_KEY_PATTERN)
    flag_key: str = Field(..., pattern=_RESOURCE_KEY_PATTERN)
    status: Literal["scheduled", "running", "completed", "stopped"]
    control_variant: str = Field(..., min_length=1, max_length=128)
    variants: list[str] = Field(..., min_length=2, max_length=10)
    metric_event: str = Field(..., min_length=1)
    metric_direction: Literal["increase", "decrease"]
    enrollment_mode: Literal["all", "targeted"]
    minimum_exposure_config_version: int = Field(..., ge=1, strict=True)
    statistical_plan: ConfigExperimentStatisticalPlan
    start_date: AwareDatetime
    end_date: AwareDatetime
    version: int = Field(..., ge=1)

    @model_validator(mode="after")
    def validate_contract(self) -> "ConfigExperimentAnalysis":
        if len(set(self.variants)) != len(self.variants):
            raise ValueError("variants must contain unique keys")
        if self.control_variant not in self.variants:
            raise ValueError("control_variant must match a declared variant")
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


class ExperimentNotFound(RuntimeError):
    """Config has no experiment with this project-scoped key."""


class ExperimentNotAnalyzable(RuntimeError):
    """Config rejected analysis for the experiment's lifecycle state."""


class ConfigServiceUnavailable(RuntimeError):
    """Authoritative experiment metadata could not be obtained safely."""


def _config_error_detail(response: httpx.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        return fallback
    if isinstance(payload, dict):
        for field in ("detail", "message"):
            if isinstance(payload.get(field), str):
                return payload[field]
    return fallback


async def fetch_experiment_analysis(
    project_id: str,
    experiment_key: str,
    api_key: str,
) -> ConfigExperimentAnalysis:
    """Resolve one experiment using the already-verified caller credential."""
    match = _API_KEY_PATTERN.fullmatch(api_key)
    if match is None or match.group("project_id") != project_id:
        raise ConfigServiceUnavailable(
            "Validated project credential is unavailable for Config delegation"
        )

    path = f"/v1/experiments/{quote(experiment_key, safe='')}/analysis"
    try:
        async with httpx.AsyncClient(
            base_url=CONFIG_SERVICE_URL,
            timeout=_TIMEOUT_SECONDS,
            headers={"X-API-Key": api_key},
        ) as client:
            response = await client.get(path)
    except httpx.RequestError as exc:
        raise ConfigServiceUnavailable("Config service request failed") from exc

    if response.status_code == 404:
        raise ExperimentNotFound(
            _config_error_detail(response, f"Experiment '{experiment_key}' was not found")
        )
    if response.status_code == 409:
        raise ExperimentNotAnalyzable(
            _config_error_detail(response, "Experiment is not analyzable")
        )
    if not response.is_success:
        raise ConfigServiceUnavailable(
            f"Config service returned HTTP {response.status_code}"
        )

    try:
        metadata = ConfigExperimentAnalysis.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        raise ConfigServiceUnavailable(
            "Config service returned an invalid experiment-analysis contract"
        ) from exc
    if metadata.key != experiment_key:
        raise ConfigServiceUnavailable(
            "Config service returned metadata for a different experiment"
        )
    return metadata
