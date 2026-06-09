"""Pydantic models for canonical feature gate configuration and evaluation.

The validation rules deliberately mirror ``sdk/javascript/src/flags/schema.ts``:
unknown keys are rejected (``extra="forbid"``), and a condition's ``value`` must
be present for value operators and absent for ``exists``/``not_exists``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ConditionOperator(str, Enum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    REGEX = "regex"
    IN = "in"
    NOT_IN = "not_in"
    EXISTS = "exists"
    NOT_EXISTS = "not_exists"


_PRESENCE_OPERATORS = {ConditionOperator.EXISTS, ConditionOperator.NOT_EXISTS}


class RolloutConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    percentage: float = Field(ge=0.0, le=100.0)
    bucket_by: str = Field(min_length=1)


class GateCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attribute: str = Field(min_length=1)
    operator: ConditionOperator
    value: Any = None

    @model_validator(mode="before")
    @classmethod
    def _check_value_presence(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        operator = data.get("operator")
        has_value = "value" in data
        if operator in ("exists", "not_exists"):
            if has_value:
                raise ValueError(f"operator '{operator}' must not carry a value")
        else:
            if not has_value or data.get("value") is None:
                raise ValueError(f"operator '{operator}' requires a non-null value")
        return data


class GateRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str | None = None
    conditions: list[GateCondition]
    rollout: RolloutConfig


class FallthroughConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: bool
    rollout: RolloutConfig


class GateConfig(BaseModel):
    """A single canonical feature gate definition."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    enabled: bool
    default_value: bool
    salt: str
    rules: list[GateRule]
    fallthrough: FallthroughConfig
    version: int = Field(ge=0)

    @field_validator("version")
    @classmethod
    def _version_is_int(cls, v: int) -> int:
        if isinstance(v, bool):  # bool is a subclass of int — reject it
            raise ValueError("version must be an integer")
        return v


GateConfigSource = Literal["memory", "initial_fetch", "sse", "local_storage", "none"]

GateEvaluationReason = Literal[
    "not_found",
    "invalid_config",
    "disabled",
    "error",
    "rule_match",
    "rule_rollout",
    "fallthrough",
    "fallthrough_rollout",
]


class EvalContext(BaseModel):
    """The identity + attributes a gate is evaluated against."""

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None
    anonymous_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class GateEvaluationResult(BaseModel):
    """The fully-explained outcome of evaluating a single gate."""

    model_config = ConfigDict(extra="forbid")

    key: str
    value: bool
    reason: GateEvaluationReason
    rule_id: str = ""
    bucket: float | None = None
    rollout_percentage: float | None = None
    bucket_by: str = ""
    config_version: int = 0
    source: GateConfigSource = "none"
