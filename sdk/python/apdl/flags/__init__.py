"""Variant feature flag evaluation: hashing, models, parsing, cache, evaluator."""

from .cache import FlagCache
from .evaluator import FlagEvaluator, assign_weighted_variant
from .hash import hash_bucket, is_in_rollout, percentage_bucket
from .models import (
    ConditionOperator,
    EvalContext,
    FallthroughConfig,
    GateCondition,
    GateConfig,
    GateEvaluationResult,
    GateRule,
    RolloutConfig,
    VariantConfig,
)
from .parse import (
    FlagConfigParseResult,
    parse_flag_config_result,
    parse_flag_configs,
)

__all__ = [
    "FlagCache",
    "FlagEvaluator",
    "assign_weighted_variant",
    "hash_bucket",
    "is_in_rollout",
    "percentage_bucket",
    "ConditionOperator",
    "EvalContext",
    "FallthroughConfig",
    "GateCondition",
    "GateConfig",
    "GateEvaluationResult",
    "GateRule",
    "RolloutConfig",
    "VariantConfig",
    "FlagConfigParseResult",
    "parse_flag_config_result",
    "parse_flag_configs",
]
