"""Pydantic models for canonical variant feature flag config and evaluation.

The validation rules deliberately mirror ``sdk/javascript/src/flags/schema.ts`` and
``services/config/app/models/schemas.py``: unknown keys are rejected
(``extra="forbid"``), a condition's ``value`` must be present for value operators and
absent for ``exists``/``not_exists``, and every flag carries a non-empty ``variants``
list whose weights are relative non-negative integers and whose keys are unique, with a
``default_variant`` drawn from them. There is no boolean flag type: a binary flag is two
variants (``control``/``treatment``).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    name: str = ""
    conditions: list[GateCondition]
    rollout: RolloutConfig


class VariantConfig(BaseModel):
    """One weighted variant. Weights are relative non-negative integers."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    # ``strict`` rejects floats (incl. ``1.0``), booleans, and numeric strings,
    # matching ``services/config/app/models/schemas.py``.
    weight: int = Field(ge=0, strict=True)


class FallthroughConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rollout: RolloutConfig


def validate_variants(variants: list[VariantConfig], default_variant: str) -> None:
    """Enforce the canonical variant invariants for a flag config.

    Shared by :class:`GateConfig`; mirrors ``validate_variants`` in the config
    service so the SDK and server agree on what a valid variant set is.
    """
    if not variants:
        raise ValueError("variants must contain at least one variant")

    keys: set[str] = set()
    total_weight = 0
    for variant in variants:
        if variant.key in keys:
            raise ValueError("variants must contain unique keys")
        keys.add(variant.key)
        total_weight += variant.weight

    if total_weight <= 0:
        raise ValueError("variant weights must contain at least one positive weight")
    if default_variant not in keys:
        raise ValueError("default_variant must match a variant key")


class GateConfig(BaseModel):
    """A single canonical variant feature flag definition."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    enabled: bool
    default_variant: str = Field(min_length=1)
    variants: list[VariantConfig]
    salt: str
    rules: list[GateRule]
    fallthrough: FallthroughConfig
    # ``strict`` rejects booleans and floats; ``ge=1`` matches the JS parser.
    version: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def _validate_variant_config(self) -> GateConfig:
        validate_variants(self.variants, self.default_variant)
        return self


GateConfigSource = Literal["memory", "initial_fetch", "sse", "local_storage", "server"]

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
    """The identity + attributes a flag is evaluated against."""

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None
    anonymous_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class GateEvaluationResult(BaseModel):
    """The fully-explained outcome of evaluating a single flag.

    Detail fields use ``None`` (never ``""``/``0`` sentinels) when they do not
    apply. ``variant`` is ``None`` only for ``not_found`` and ``invalid_config``.
    """

    model_config = ConfigDict(extra="forbid")

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
