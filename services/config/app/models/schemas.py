"""Pydantic models for gates and experiments."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Base model for public API contracts."""

    model_config = ConfigDict(extra="forbid")


ConditionOperator = Literal[
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
    "regex",
    "in",
    "not_in",
    "exists",
    "not_exists",
]


class GateCondition(StrictModel):
    attribute: str = Field(..., min_length=1)
    operator: ConditionOperator
    value: Any | None = None

    @model_validator(mode="after")
    def validate_value(self):
        if self.operator in {"exists", "not_exists"}:
            if self.value is not None:
                raise ValueError(f"{self.operator} conditions must not include value")
            return self
        if self.value is None:
            raise ValueError(f"{self.operator} conditions require value")
        return self


class RolloutConfig(StrictModel):
    percentage: float = Field(..., ge=0.0, le=100.0)
    bucket_by: str = Field(default="user_id", min_length=1)


class GateRule(StrictModel):
    id: str = Field(..., min_length=1)
    name: str = ""
    conditions: list[GateCondition] = Field(default_factory=list)
    rollout: RolloutConfig


class FallthroughConfig(StrictModel):
    value: bool = False
    rollout: RolloutConfig = Field(
        default_factory=lambda: RolloutConfig(percentage=0.0, bucket_by="user_id")
    )


class GuardrailConfig(StrictModel):
    metric: str = Field(..., min_length=1)
    threshold: str = Field(..., min_length=1)
    scope: str = ""
    minimum_exposures: int = Field(default=0, ge=0)
    window_minutes: int = Field(default=10, ge=1)


class FlagConfig(BaseModel):
    key: str
    project_id: str = ""
    name: str = ""
    enabled: bool = False
    description: str = ""
    default_value: bool = False
    rules: list[GateRule] = Field(default_factory=list)
    fallthrough: FallthroughConfig = Field(default_factory=FallthroughConfig)
    salt: str = ""
    client_exposed: bool = True
    auto_disable: bool = True
    guardrails: list[GuardrailConfig] = Field(default_factory=list)
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    archived_at: str | None = None


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
    attributes: dict[str, str] = Field(default_factory=dict)


class EvalResult(BaseModel):
    key: str
    value: bool = False
    reason: str = ""
    rule_id: str = ""
    bucket: float | None = None
    rollout_percentage: float | None = None
    bucket_by: str = ""
    config_version: int = 0


# ---------- Admin request bodies ----------

class FlagCreate(StrictModel):
    key: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    enabled: bool = False
    description: str = ""
    default_value: bool = False
    rules: list[GateRule] = Field(default_factory=list)
    fallthrough: FallthroughConfig = Field(default_factory=FallthroughConfig)
    client_exposed: bool = True
    auto_disable: bool = True
    guardrails: list[GuardrailConfig] = Field(default_factory=list)


class FlagUpdate(StrictModel):
    version: int = Field(..., ge=1)
    enabled: bool | None = None
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    default_value: bool | None = None
    rules: list[GateRule] | None = None
    fallthrough: FallthroughConfig | None = None
    client_exposed: bool | None = None
    auto_disable: bool | None = None
    guardrails: list[GuardrailConfig] | None = None


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
