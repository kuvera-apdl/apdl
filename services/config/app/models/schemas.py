"""Pydantic models for gates and experiments."""

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from math import ceil, isfinite, sqrt
from statistics import NormalDist
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.flags.targeting_contract import (
    MAX_CONDITIONS_PER_RULE,
    MAX_IDENTIFIER_LENGTH,
    MAX_MEMBERSHIP_VALUES,
    MAX_RULES,
    MAX_STRING_LENGTH,
    PRESENCE_OPERATORS,
    is_bounded_string,
    is_condition_value_valid,
    is_identifier,
    is_json_number,
)
from app.flags.variant_contract import (
    MAX_VARIANTS,
    MAX_VARIANT_WEIGHT,
    validate_variant_weight_contract,
)


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
    "in",
    "not_in",
    "exists",
    "not_exists",
]

GuardrailMetric = Literal["frontend_error_rate", "frontend_error_count"]
GuardrailThreshold = Literal["2x_baseline", "at_least_one"]
EvaluationMode = Literal["client", "server", "both"]
FlagState = Literal["draft", "active", "disabled", "archived"]
WritableFlagState = Literal["draft", "active", "disabled"]
ExperimentStatus = Literal[
    "draft",
    "scheduled",
    "running",
    "completed",
    "stopped",
]
ExperimentCreateStatus = Literal["draft", "scheduled", "running"]
MAX_EXPERIMENT_DURATION_DAYS = 90
MAX_EXPERIMENT_DURATION = timedelta(days=MAX_EXPERIMENT_DURATION_DAYS)
EXPERIMENT_STATISTICAL_PROTOCOL = "fixed_horizon_fisher_newcombe_cc_plan_v1"
MIN_STATISTICAL_VALUE = 1e-6
MAX_NOMINAL_POWER = 0.9999
MAX_REQUIRED_SAMPLE_SIZE_PER_ARM = 10_000_000
MIN_DATA_SETTLEMENT_SECONDS = 1
MAX_DATA_SETTLEMENT_SECONDS = 86_400
MAX_ANALYTICS_WINDOW_MINUTES = 90 * 24 * 60
RESOURCE_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
MAX_EVAL_CONTEXT_DEPTH = 4
MAX_EVAL_CONTEXT_KEYS = 100
MAX_EVAL_CONTEXT_NODES = 1000
MAX_EVAL_PAGE_LENGTH = 2048
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

GateConfigSource = Literal["memory", "initial_fetch", "sse", "local_storage", "server"]


class GateCondition(StrictModel):
    attribute: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    operator: ConditionOperator
    value: Any | None = None

    @model_validator(mode="after")
    def validate_value(self):
        if self.operator in PRESENCE_OPERATORS:
            if "value" in self.model_fields_set:
                raise ValueError(f"{self.operator} conditions must not include value")
            return self
        if "value" not in self.model_fields_set:
            raise ValueError(f"{self.operator} conditions require value")
        if not is_condition_value_valid(self.operator, self.value):
            raise ValueError(f"invalid value for {self.operator} condition")
        return self


class RolloutConfig(StrictModel):
    percentage: float = Field(
        ...,
        ge=0.0,
        le=100.0,
        strict=True,
        allow_inf_nan=False,
    )
    bucket_by: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)


class VariantConfig(StrictModel):
    key: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    weight: int = Field(
        ...,
        ge=0,
        le=MAX_VARIANT_WEIGHT,
        strict=True,
    )


class GateRule(StrictModel):
    id: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    name: str = Field(default="", max_length=MAX_STRING_LENGTH)
    conditions: list[GateCondition] = Field(
        default_factory=list,
        max_length=MAX_CONDITIONS_PER_RULE,
    )
    rollout: RolloutConfig


class ExperimentTargetingRule(StrictModel):
    """Eligibility-only rule for experiment enrollment.

    Experiment traffic allocation has exactly one authority:
    ``traffic_percentage``.  The backing flag projection supplies that rollout;
    accepting a second per-rule rollout here would make enrollment ambiguous.
    """

    id: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    name: str = Field(..., max_length=MAX_STRING_LENGTH)
    conditions: list[GateCondition] = Field(
        default_factory=list,
        max_length=MAX_CONDITIONS_PER_RULE,
    )


class FallthroughConfig(StrictModel):
    rollout: RolloutConfig


class GuardrailConfig(StrictModel):
    metric: GuardrailMetric
    threshold: GuardrailThreshold
    scope: str = ""
    minimum_exposures: int = Field(default=0, ge=0)
    window_minutes: int = Field(
        default=10,
        ge=1,
        le=MAX_ANALYTICS_WINDOW_MINUTES,
    )

    @model_validator(mode="after")
    def validate_metric_threshold(self):
        if self.metric == "frontend_error_rate" and self.threshold != "2x_baseline":
            raise ValueError("frontend_error_rate guardrails require threshold '2x_baseline'")
        if self.metric == "frontend_error_count" and self.threshold != "at_least_one":
            raise ValueError("frontend_error_count guardrails require threshold 'at_least_one'")
        return self


def default_variants() -> list[VariantConfig]:
    return [
        VariantConfig(key="control", weight=1),
        VariantConfig(key="treatment", weight=1),
    ]


def validate_variants(
    variants: list[VariantConfig],
    default_variant: str,
) -> None:
    validate_variant_weights(variants)
    if default_variant not in {variant.key for variant in variants}:
        raise ValueError("default_variant must match a variant key")


def validate_variant_weights(variants: list[VariantConfig]) -> None:
    validate_variant_weight_contract([variant.weight for variant in variants])

    keys: set[str] = set()
    for variant in variants:
        if variant.key in keys:
            raise ValueError("variants must contain unique keys")
        keys.add(variant.key)


def validate_flag_variant_config(flag: Mapping[str, Any]) -> None:
    """Validate canonical variants and rollout fields before persistence."""
    default_variant = flag.get("default_variant")
    if not isinstance(default_variant, str) or not default_variant:
        raise ValueError("default_variant must be a non-empty string")

    variants = flag.get("variants")
    if not isinstance(variants, list):
        raise ValueError("variants must contain at least one variant")

    parsed_variants = [
        VariantConfig.model_validate(
            variant.model_dump(
                mode="python",
                exclude_unset=True,
                warnings="none",
            )
            if isinstance(variant, VariantConfig)
            else variant
        )
        for variant in variants
    ]
    validate_variants(parsed_variants, default_variant)

    rules = flag.get("rules")
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    if len(rules) > MAX_RULES:
        raise ValueError(f"rules must contain at most {MAX_RULES} entries")
    for rule in rules:
        GateRule.model_validate(
            rule.model_dump(
                mode="python",
                exclude_unset=True,
                warnings="none",
            )
            if isinstance(rule, GateRule)
            else rule
        )

    fallthrough = flag.get("fallthrough")
    FallthroughConfig.model_validate(
        fallthrough.model_dump(
            mode="python",
            exclude_unset=True,
            warnings="none",
        )
        if isinstance(fallthrough, FallthroughConfig)
        else fallthrough
    )


class VariantFlagMixin(StrictModel):
    default_variant: str = Field(
        ...,
        min_length=1,
        max_length=MAX_IDENTIFIER_LENGTH,
    )
    variants: list[VariantConfig] = Field(
        ...,
        min_length=1,
        max_length=MAX_VARIANTS,
    )

    @model_validator(mode="after")
    def validate_variant_config(self):
        # Nested rules and fallthrough have already been parsed by Pydantic at
        # this point. Re-serializing and parsing them again would materialize
        # optional fields that were deliberately omitted (for example, the
        # absent ``value`` on presence operators).
        validate_variants(self.variants, self.default_variant)
        return self


class FlagConfig(VariantFlagMixin):
    key: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    project_id: str = ""
    name: str = ""
    state: FlagState = "draft"
    owners: list[str] = Field(default_factory=list)
    review_by: date | None = None
    enabled: bool = False
    description: str = ""
    rules: list[GateRule] = Field(default_factory=list, max_length=MAX_RULES)
    fallthrough: FallthroughConfig
    salt: str = Field(default="", max_length=MAX_STRING_LENGTH)
    evaluation_mode: EvaluationMode = "client"
    auto_disable: Literal[False] = False
    guardrails: list[GuardrailConfig] = Field(default_factory=list)
    disabled_reason: str = ""
    disabled_by: str = ""
    disabled_at: str | None = None
    version: int = Field(default=1, ge=1)
    created_at: str = ""
    updated_at: str = ""
    archived_at: str | None = None


class ClientFlagConfig(VariantFlagMixin):
    key: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    enabled: bool
    salt: str = Field(..., max_length=MAX_STRING_LENGTH)
    rules: list[GateRule] = Field(..., max_length=MAX_RULES)
    fallthrough: FallthroughConfig
    version: int = Field(..., ge=1)


class ExperimentConfig(BaseModel):
    """Mirror of a stored ``experiments`` row (canonical columns)."""

    key: str
    project_id: str = ""
    status: ExperimentStatus = "draft"
    description: str = ""
    flag_key: str = ""
    default_variant: str = "control"
    variants_json: str = "[]"
    targeting_rules_json: str = "[]"
    primary_metric_json: str = "{}"
    statistical_plan: dict[str, Any] | None = None
    traffic_percentage: float = Field(
        default=100.0,
        ge=0.0,
        le=100.0,
        strict=True,
        allow_inf_nan=False,
    )
    minimum_exposure_config_version: int | None = Field(default=None, ge=1)
    start_date: AwareDatetime | None = None
    end_date: AwareDatetime | None = None
    version: int = Field(default=1, ge=1)
    created_at: str = ""
    updated_at: str = ""
    archived_at: str | None = None
    archived_by: str | None = None


def _validate_context_value(
    value: Any,
    *,
    depth: int = 0,
    budget: dict[str, int] | None = None,
) -> bool:
    """Bound JSON context values without coercing their types."""
    if budget is None:
        budget = {"keys": 0, "nodes": 0}
    budget["nodes"] += 1
    if budget["nodes"] > MAX_EVAL_CONTEXT_NODES:
        return False
    if value is None or isinstance(value, bool) or is_json_number(value):
        return True
    if isinstance(value, str):
        return is_bounded_string(value)
    if depth >= MAX_EVAL_CONTEXT_DEPTH:
        return False
    if isinstance(value, list):
        return len(value) <= MAX_MEMBERSHIP_VALUES and all(
            _validate_context_value(
                item,
                depth=depth + 1,
                budget=budget,
            )
            for item in value
        )
    if isinstance(value, dict):
        budget["keys"] += len(value)
        if budget["keys"] > MAX_EVAL_CONTEXT_KEYS:
            return False
        return len(value) <= MAX_MEMBERSHIP_VALUES and all(
            is_identifier(key)
            and _validate_context_value(
                item,
                depth=depth + 1,
                budget=budget,
            )
            for key, item in value.items()
        )
    return False


class EvalContext(StrictModel):
    user_id: str = Field(default="", max_length=MAX_IDENTIFIER_LENGTH)
    anonymous_id: str = Field(default="", max_length=MAX_IDENTIFIER_LENGTH)
    attributes: dict[str, Any] = Field(
        default_factory=dict,
        max_length=MAX_MEMBERSHIP_VALUES,
    )

    @model_validator(mode="after")
    def validate_attributes(self):
        budget = {"keys": len(self.attributes), "nodes": 1}
        if not all(
            is_identifier(key)
            and _validate_context_value(value, budget=budget)
            for key, value in self.attributes.items()
        ) or budget["keys"] > MAX_EVAL_CONTEXT_KEYS:
            raise ValueError("attributes must contain bounded JSON values")
        return self


class EvalResult(StrictModel):
    key: str
    variant: str | None = None
    reason: str = ""
    rule_id: str | None = None
    rollout_bucket: float | None = None
    variant_bucket: float | None = None
    rollout_percentage: float | None = None
    bucket_by: str | None = None
    config_version: int | None = None
    source: GateConfigSource | None = None


class GateEvaluateRequest(StrictModel):
    project_id: str = Field(
        ...,
        min_length=1,
        max_length=MAX_IDENTIFIER_LENGTH,
    )
    key: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    context: EvalContext = Field(default_factory=EvalContext)
    log_exposure: bool = True
    session_id: str = Field(default="", max_length=MAX_IDENTIFIER_LENGTH)
    message_id: str = Field(default="", max_length=MAX_IDENTIFIER_LENGTH)
    page: str = Field(default="", max_length=MAX_EVAL_PAGE_LENGTH)
    component: str = Field(default="", max_length=MAX_STRING_LENGTH)

    @model_validator(mode="after")
    def validate_exposure_idempotency(self):
        if self.log_exposure and (
            not self.message_id or self.message_id != self.message_id.strip()
        ):
            raise ValueError(
                "log_exposure requires a stable nonblank message_id"
            )
        return self


class GateEvaluateResponse(StrictModel):
    key: str
    variant: str | None = None
    reason: GateEvaluationReason
    rule_id: str | None = None
    rollout_bucket: float | None = None
    variant_bucket: float | None = None
    rollout_percentage: float | None = None
    bucket_by: str | None = None
    config_version: int | None = None
    source: GateConfigSource | None = None


# ---------- Admin request bodies ----------

class FlagCreate(VariantFlagMixin):
    key: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    name: str = Field(..., min_length=1)
    state: WritableFlagState = "draft"
    owners: list[str] = Field(default_factory=list)
    review_by: date | None = None
    enabled: bool = False
    description: str = ""
    rules: list[GateRule] = Field(default_factory=list, max_length=MAX_RULES)
    fallthrough: FallthroughConfig
    evaluation_mode: EvaluationMode = "client"
    auto_disable: Literal[False] = False
    guardrails: list[GuardrailConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lifecycle(self):
        validate_owners(self.owners)
        validate_state_enabled(self.state, self.enabled)
        return self


class FlagUpdate(StrictModel):
    version: int = Field(..., ge=1)
    owners: list[str] | None = None
    review_by: date | None = None
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    default_variant: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_IDENTIFIER_LENGTH,
    )
    variants: list[VariantConfig] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_VARIANTS,
    )
    rules: list[GateRule] | None = Field(default=None, max_length=MAX_RULES)
    fallthrough: FallthroughConfig | None = None
    evaluation_mode: EvaluationMode | None = None
    auto_disable: Literal[False] | None = None
    guardrails: list[GuardrailConfig] | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self):
        nullable_fields = {"review_by"}
        for field in self.model_fields_set - nullable_fields:
            if getattr(self, field) is None:
                raise ValueError(f"{field} must not be null")
        if self.owners is not None:
            validate_owners(self.owners)
        if self.variants is not None:
            validate_variant_weights(self.variants)
        if self.variants is not None and self.default_variant is not None:
            validate_variants(self.variants, self.default_variant)
        return self


class FlagTransition(StrictModel):
    version: int = Field(..., ge=1)
    target_state: Literal["draft", "active"]


class FlagDisable(StrictModel):
    version: int = Field(..., ge=1)
    reason: Literal["guardrail_failed", "experiment_rollback"] = "guardrail_failed"
    evidence: dict[str, Any] = Field(default_factory=dict)


class FlagCleanup(StrictModel):
    version: int = Field(..., ge=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


def validate_owners(owners: list[str]) -> None:
    for owner in owners:
        if not owner.strip():
            raise ValueError("owners must contain non-empty strings")


def validate_state_enabled(state: str, enabled: bool) -> None:
    expected_enabled = state == "active"
    if enabled != expected_enabled:
        raise ValueError(f"state '{state}' requires enabled={expected_enabled}")


class ExperimentVariant(StrictModel):
    """A variant as authored on an experiment.

    Carries optional display fields (``description``) that the backing flag's
    strict ``VariantConfig`` does not; projected down to ``{key, weight}`` when
    the flag is derived.
    """

    key: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    weight: int = Field(
        ...,
        gt=0,
        le=MAX_VARIANT_WEIGHT,
        strict=True,
    )
    description: str = ""


class ExperimentMetric(StrictModel):
    """Canonical primary conversion-metric descriptor.

    ``type`` is fixed to ``conversion``; ``event`` drives the Query service's
    exposure-to-conversion join and ``direction`` remains display metadata.
    """

    event: str = Field(..., min_length=1)
    type: Literal["conversion"] = "conversion"
    direction: Literal["increase", "decrease"] = "increase"


class ExperimentStatisticalPlan(StrictModel):
    """Immutable fixed-horizon design declared before traffic is observed."""

    protocol: Literal[EXPERIMENT_STATISTICAL_PROTOCOL]
    baseline_conversion_rate: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        strict=True,
        allow_inf_nan=False,
    )
    minimum_detectable_effect: float = Field(
        ...,
        ge=MIN_STATISTICAL_VALUE,
        le=1.0,
        strict=True,
        allow_inf_nan=False,
    )
    significance_level: float = Field(
        ...,
        ge=MIN_STATISTICAL_VALUE,
        le=0.5,
        strict=True,
        allow_inf_nan=False,
    )
    nominal_power: float = Field(
        ...,
        gt=0.5,
        le=MAX_NOMINAL_POWER,
        strict=True,
        allow_inf_nan=False,
    )
    required_sample_size_per_arm: int = Field(
        ...,
        ge=2,
        le=MAX_REQUIRED_SAMPLE_SIZE_PER_ARM,
        strict=True,
    )
    data_settlement_seconds: int = Field(
        ...,
        ge=MIN_DATA_SETTLEMENT_SECONDS,
        le=MAX_DATA_SETTLEMENT_SECONDS,
        strict=True,
    )


def prospective_sample_size_per_arm(
    *,
    baseline_conversion_rate: float,
    minimum_detectable_effect: float,
    significance_level: float,
    nominal_power: float,
    treatment_count: int,
    direction: Literal["increase", "decrease"],
) -> int:
    """Continuity-corrected nominal target for the fixed-horizon protocol.

    The base term is the standard two-proportion prospective calculation with
    family-wise alpha divided across every declared treatment comparison.
    Fisher's conditional test is discrete and can be materially more
    conservative at sparse counts, so a Casagrande-style continuity correction
    is applied before rounding. ``nominal_power`` is a planning input, not a
    guarantee of exact achieved Fisher power. Snapshot inference never
    substitutes this planning approximation for the declared Fisher test.
    """
    if treatment_count < 1:
        raise ValueError("statistical planning requires at least one treatment")
    signed_effect = (
        minimum_detectable_effect
        if direction == "increase"
        else -minimum_detectable_effect
    )
    treatment_rate = baseline_conversion_rate + signed_effect
    if not 0.0 <= treatment_rate <= 1.0:
        raise ValueError(
            "minimum_detectable_effect is incompatible with the baseline "
            f"conversion rate and {direction!r} metric direction"
        )

    per_comparison_alpha = significance_level / treatment_count
    pooled_rate = (baseline_conversion_rate + treatment_rate) / 2.0
    z_alpha = NormalDist().inv_cdf(1.0 - per_comparison_alpha / 2.0)
    z_beta = NormalDist().inv_cdf(nominal_power)
    numerator = (
        z_alpha * sqrt(2.0 * pooled_rate * (1.0 - pooled_rate))
        + z_beta
        * sqrt(
            baseline_conversion_rate * (1.0 - baseline_conversion_rate)
            + treatment_rate * (1.0 - treatment_rate)
        )
    ) ** 2
    asymptotic_n = numerator / (minimum_detectable_effect**2)
    continuity_corrected_n = (
        asymptotic_n
        / 4.0
        * (
            1.0
            + sqrt(
                1.0
                + 4.0 / (asymptotic_n * minimum_detectable_effect)
            )
        )
        ** 2
    )
    if (
        not isfinite(continuity_corrected_n)
        or continuity_corrected_n > MAX_REQUIRED_SAMPLE_SIZE_PER_ARM
    ):
        raise ValueError(
            "declared statistical plan exceeds the supported per-arm sample target"
        )
    return max(2, ceil(continuity_corrected_n))


def validate_statistical_plan(
    *,
    status: str,
    statistical_plan: ExperimentStatisticalPlan | None,
    primary_metric: ExperimentMetric | None,
    variant_count: int,
) -> None:
    """Require and validate the immutable prospective decision contract."""
    if statistical_plan is None:
        if status in {"scheduled", "running"}:
            raise ValueError(f"{status} experiments require statistical_plan")
        return
    if primary_metric is None:
        raise ValueError("statistical_plan requires primary_metric")

    minimum_target = prospective_sample_size_per_arm(
        baseline_conversion_rate=statistical_plan.baseline_conversion_rate,
        minimum_detectable_effect=statistical_plan.minimum_detectable_effect,
        significance_level=statistical_plan.significance_level,
        nominal_power=statistical_plan.nominal_power,
        treatment_count=variant_count - 1,
        direction=primary_metric.direction,
    )
    if statistical_plan.required_sample_size_per_arm < minimum_target:
        raise ValueError(
            "required_sample_size_per_arm must be at least "
            f"{minimum_target} for the declared statistical plan"
        )


def _projected_variants(variants: list[ExperimentVariant]) -> list[VariantConfig]:
    return [VariantConfig(key=v.key, weight=v.weight) for v in variants]


class ExperimentAnalysis(StrictModel):
    """Authoritative metadata required by the experiment-analysis service."""

    key: str = Field(..., pattern=RESOURCE_KEY_PATTERN)
    flag_key: str = Field(..., pattern=RESOURCE_KEY_PATTERN)
    status: Literal["scheduled", "running", "completed", "stopped"]
    control_variant: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    variants: list[str] = Field(
        ...,
        min_length=2,
        max_length=MAX_VARIANTS,
    )
    metric_event: str = Field(..., min_length=1)
    metric_direction: Literal["increase", "decrease"]
    enrollment_mode: Literal["all", "targeted"]
    minimum_exposure_config_version: int = Field(..., ge=1)
    statistical_plan: ExperimentStatisticalPlan
    start_date: AwareDatetime
    end_date: AwareDatetime
    version: int = Field(..., ge=1)

    @model_validator(mode="after")
    def validate_analysis_contract(self):
        if len(set(self.variants)) != len(self.variants):
            raise ValueError("variants must contain unique keys")
        if any(not is_identifier(variant) for variant in self.variants):
            raise ValueError("variants must contain bounded non-empty keys")
        if self.control_variant not in self.variants:
            raise ValueError("control_variant must match a variant key")
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        if self.end_date - self.start_date > MAX_EXPERIMENT_DURATION:
            raise ValueError(
                f"experiment duration must not exceed {MAX_EXPERIMENT_DURATION_DAYS} days"
            )
        return self


class ExperimentCreate(StrictModel):
    key: str = Field(..., pattern=RESOURCE_KEY_PATTERN)
    flag_key: str | None = Field(default=None, pattern=RESOURCE_KEY_PATTERN)
    status: ExperimentCreateStatus = "draft"
    description: str = ""
    traffic_percentage: float = Field(
        default=100.0,
        ge=0.0,
        le=100.0,
        strict=True,
        allow_inf_nan=False,
    )
    start_date: AwareDatetime | None = None
    end_date: AwareDatetime | None = None
    variants: list[ExperimentVariant] = Field(
        ...,
        min_length=2,
        max_length=MAX_VARIANTS,
    )
    default_variant: str = Field(
        ...,
        min_length=1,
        max_length=MAX_IDENTIFIER_LENGTH,
    )
    primary_metric: ExperimentMetric | None = None
    statistical_plan: ExperimentStatisticalPlan | None = None
    targeting_rules: list[ExperimentTargetingRule] = Field(
        default_factory=list,
        max_length=MAX_RULES,
    )

    @model_validator(mode="after")
    def validate_experiment(self):
        projected = _projected_variants(self.variants)
        validate_variant_weights(projected)
        validate_variants(projected, self.default_variant)
        validate_experiment_lifecycle(
            status=self.status,
            start_date=self.start_date,
            end_date=self.end_date,
            primary_metric=self.primary_metric,
        )
        validate_statistical_plan(
            status=self.status,
            statistical_plan=self.statistical_plan,
            primary_metric=self.primary_metric,
            variant_count=len(self.variants),
        )
        return self


class ExperimentUpdate(StrictModel):
    version: int = Field(..., ge=1)
    status: ExperimentStatus | None = None
    description: str | None = None
    traffic_percentage: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        strict=True,
        allow_inf_nan=False,
    )
    start_date: AwareDatetime | None = None
    end_date: AwareDatetime | None = None
    variants: list[ExperimentVariant] | None = Field(
        default=None,
        min_length=2,
        max_length=MAX_VARIANTS,
    )
    default_variant: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_IDENTIFIER_LENGTH,
    )
    primary_metric: ExperimentMetric | None = None
    statistical_plan: ExperimentStatisticalPlan | None = None
    targeting_rules: list[ExperimentTargetingRule] | None = Field(
        default=None,
        max_length=MAX_RULES,
    )

    @model_validator(mode="after")
    def validate_experiment(self):
        nullable_fields = {
            "start_date",
            "end_date",
            "primary_metric",
            "statistical_plan",
        }
        for field in self.model_fields_set - nullable_fields:
            if getattr(self, field) is None:
                raise ValueError(f"{field} must not be null")
        if self.variants is not None:
            projected = _projected_variants(self.variants)
            validate_variant_weights(projected)
            if self.default_variant is not None:
                validate_variants(projected, self.default_variant)
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date <= self.start_date
        ):
            raise ValueError("end_date must be after start_date")
        return self


def validate_experiment_lifecycle(
    *,
    status: str,
    start_date: datetime | None,
    end_date: datetime | None,
    primary_metric: ExperimentMetric | None,
    now: datetime | None = None,
) -> None:
    """Validate the complete experiment serving window and launch state."""
    if end_date is not None and start_date is None:
        raise ValueError("end_date requires start_date")
    if start_date is not None and end_date is not None and end_date <= start_date:
        raise ValueError("end_date must be after start_date")
    if (
        start_date is not None
        and end_date is not None
        and end_date - start_date > MAX_EXPERIMENT_DURATION
    ):
        raise ValueError(
            f"experiment duration must not exceed {MAX_EXPERIMENT_DURATION_DAYS} days"
        )
    if status not in {"scheduled", "running"}:
        return
    if start_date is None or end_date is None:
        raise ValueError(f"{status} experiments require start_date and end_date")
    if primary_metric is None:
        raise ValueError(f"{status} experiments require primary_metric")
    current = now or datetime.now(timezone.utc)
    if status == "scheduled" and start_date <= current:
        raise ValueError("scheduled experiments require a future start_date")
    if status == "running" and start_date > current:
        raise ValueError("future experiments must use status 'scheduled'")
    if status == "running" and end_date <= current:
        raise ValueError("running experiments require a future end_date")
