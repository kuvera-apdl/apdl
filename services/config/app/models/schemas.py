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

GuardrailMetric = Literal["frontend_error_rate", "frontend_error_count"]
GuardrailThreshold = Literal["2x_baseline", "at_least_one"]
EvaluationMode = Literal["client", "server", "both"]
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
    metric: GuardrailMetric
    threshold: GuardrailThreshold
    scope: str = ""
    minimum_exposures: int = Field(default=0, ge=0)
    window_minutes: int = Field(default=10, ge=1)

    @model_validator(mode="after")
    def validate_metric_threshold(self):
        if self.metric == "frontend_error_rate" and self.threshold != "2x_baseline":
            raise ValueError("frontend_error_rate guardrails require threshold '2x_baseline'")
        if self.metric == "frontend_error_count" and self.threshold != "at_least_one":
            raise ValueError("frontend_error_count guardrails require threshold 'at_least_one'")
        return self


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
    evaluation_mode: EvaluationMode = "client"
    auto_disable: bool = True
    guardrails: list[GuardrailConfig] = Field(default_factory=list)
    disabled_reason: str = ""
    disabled_by: str = ""
    disabled_at: str | None = None
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


class EvalContext(StrictModel):
    user_id: str = ""
    anonymous_id: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)


class EvalResult(BaseModel):
    key: str
    value: bool = False
    reason: str = ""
    rule_id: str = ""
    bucket: float | None = None
    rollout_percentage: float | None = None
    bucket_by: str = ""
    config_version: int = 0


class GateEvaluateRequest(StrictModel):
    project_id: str = Field(..., min_length=1)
    key: str = Field(..., min_length=1)
    context: EvalContext = Field(default_factory=EvalContext)
    log_exposure: bool = True
    session_id: str = ""
    message_id: str = ""
    page: str = ""


class GateEvaluateResponse(StrictModel):
    key: str
    value: bool = False
    reason: GateEvaluationReason
    rule_id: str = ""
    bucket: float | None = None
    rollout_percentage: float | None = None
    bucket_by: str = ""
    config_version: int = 0
    source: Literal["server"] = "server"


# ---------- Admin request bodies ----------

class FlagCreate(StrictModel):
    key: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    enabled: bool = False
    description: str = ""
    default_value: bool = False
    rules: list[GateRule] = Field(default_factory=list)
    fallthrough: FallthroughConfig = Field(default_factory=FallthroughConfig)
    evaluation_mode: EvaluationMode = "client"
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
    evaluation_mode: EvaluationMode | None = None
    auto_disable: bool | None = None
    guardrails: list[GuardrailConfig] | None = None


class FlagDisable(StrictModel):
    reason: Literal["guardrail_failed"] = "guardrail_failed"
    source: Literal["system", "admin"] = "system"
    evidence: dict[str, Any] = Field(default_factory=dict)


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
