"""Pydantic models for flags and experiments."""

from typing import Any

from pydantic import BaseModel, Field


class FlagConfig(BaseModel):
    key: str
    project_id: str = ""
    enabled: bool = False
    description: str = ""
    variant_type: str = "boolean"
    default_value: str = "false"
    rules_json: str = "[]"
    variants_json: str = "[]"
    rollout_percentage: float = 100.0
    created_at: str = ""
    updated_at: str = ""


class ExperimentConfig(BaseModel):
    key: str
    project_id: str = ""
    status: str = "draft"
    description: str = ""
    variants_json: str = "[]"
    targeting_rules_json: str = "[]"
    traffic_percentage: float = 100.0
    start_date: str = ""
    end_date: str = ""
    created_at: str = ""
    updated_at: str = ""


class EvalContext(BaseModel):
    user_id: str = ""
    anonymous_id: str = ""
    attributes: dict[str, str] = {}


class EvalResult(BaseModel):
    key: str
    enabled: bool = False
    value: str = ""
    variant: str = ""
    reason: str = ""


# ---------- Admin request bodies ----------

class FlagCreate(BaseModel):
    key: str = Field(..., min_length=1)
    enabled: bool = False
    description: str = ""
    variant_type: str = "boolean"
    default_value: str = "false"
    rollout_percentage: float = Field(default=100.0, ge=0.0, le=100.0)
    rules: list[Any] = Field(default_factory=list)
    variants: list[Any] = Field(default_factory=list)


class FlagUpdate(BaseModel):
    enabled: bool | None = None
    description: str | None = None
    variant_type: str | None = None
    default_value: str | None = None
    rollout_percentage: float | None = Field(default=None, ge=0.0, le=100.0)
    rules: list[Any] | None = None
    variants: list[Any] | None = None


class ExperimentCreate(BaseModel):
    key: str = Field(..., min_length=1)
    status: str = "draft"
    description: str = ""
    traffic_percentage: float = Field(default=100.0, ge=0.0, le=100.0)
    start_date: str = ""
    end_date: str = ""
    variants: list[Any] = Field(default_factory=list)
    targeting_rules: list[Any] = Field(default_factory=list)


class ExperimentUpdate(BaseModel):
    status: str | None = None
    description: str | None = None
    traffic_percentage: float | None = Field(default=None, ge=0.0, le=100.0)
    start_date: str | None = None
    end_date: str | None = None
    variants: list[Any] | None = None
    targeting_rules: list[Any] | None = None
