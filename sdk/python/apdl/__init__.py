"""APDL — server-side Python SDK.

Quick start::

    from apdl import APDL

    client = APDL.init(
        api_key="proj_demo_0123456789abcdef",
        endpoint="https://apdl.example.com",
    )
    client.track("order_completed", {"total": 42.0}, user_id="u_123")

    if client.get_variant("new-checkout", user_id="u_123") == "treatment":
        ...

    client.shutdown()

Or as a context manager::

    with APDL.init(
        api_key="proj_demo_0123456789abcdef",
        endpoint="https://apdl.example.com",
    ) as client:
        client.identify("u_123", {"plan": "pro"})
"""

from __future__ import annotations

from typing import Any

from .client import APDLClient
from .config import APDLConfig
from .queue import DeliveryReport
from .flags import (
    ConditionOperator,
    EvalContext,
    FallthroughConfig,
    FlagConfigParseResult,
    GateCondition,
    GateConfig,
    GateEvaluationResult,
    GateRule,
    RolloutConfig,
    VariantConfig,
    assign_weighted_variant,
    hash_bucket,
    is_in_rollout,
    parse_flag_config_result,
    parse_flag_configs,
    percentage_bucket,
)
from .types import SDK_VERSION, IngestionEvent

__version__ = SDK_VERSION


class APDL:
    """Namespace entry point mirroring the JS SDK's ``APDL.init``."""

    @staticmethod
    def init(
        config: APDLConfig | None = None,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        **kwargs: Any,
    ) -> APDLClient:
        """Create a client from one config or explicit key and endpoint options."""
        return APDLClient(config, api_key=api_key, endpoint=endpoint, **kwargs)


__all__ = [
    "APDL",
    "APDLClient",
    "APDLConfig",
    "DeliveryReport",
    "IngestionEvent",
    "SDK_VERSION",
    "__version__",
    # Flags
    "ConditionOperator",
    "EvalContext",
    "FallthroughConfig",
    "FlagConfigParseResult",
    "GateCondition",
    "GateConfig",
    "GateEvaluationResult",
    "GateRule",
    "RolloutConfig",
    "VariantConfig",
    "assign_weighted_variant",
    "hash_bucket",
    "is_in_rollout",
    "percentage_bucket",
    "parse_flag_config_result",
    "parse_flag_configs",
]
