"""Feature gate evaluation: hashing, models, parsing, cache, and evaluator."""

from .cache import FlagCache
from .evaluator import FlagEvaluator
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
)
from .parse import (
    FlagConfigParseResult,
    parse_flag_config_result,
    parse_flag_configs,
)

__all__ = [
    "FlagCache",
    "FlagEvaluator",
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
    "FlagConfigParseResult",
    "parse_flag_config_result",
    "parse_flag_configs",
]
