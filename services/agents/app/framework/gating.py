"""Autonomy gating — the shared decision of whether an agent may act.

The four autonomy levels are a product-wide contract:

* **L1** — suggest only; never mutate anything.
* **L2** — auto-apply safe actions, route anything risky to human approval.
* **L3** — auto-apply low-risk actions, route the rest to approval.
* **L4** — full autonomy.

Every acting agent funnels its safety result through :func:`gate_action`
so the policy lives in exactly one place instead of being re-derived in
each agent's routing function.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class GateDecision(str, Enum):
    """What an agent is permitted to do with a validated action."""

    deploy = "deploy"      # apply the action now
    approve = "approve"    # hold for human approval
    halt = "halt"          # do not proceed (failed safety or suggest-only)


def gate_action(
    autonomy_level: int,
    safety_result: dict[str, Any],
    *,
    always_require_approval: bool = False,
) -> GateDecision:
    """Decide the fate of an action given autonomy level and safety result.

    Args:
        autonomy_level: The run's autonomy level (1-4).
        safety_result: A ``SafetyResult.model_dump()`` — must carry ``passed``
            and ``risk_level`` keys.
        always_require_approval: For inherently high-impact actions (e.g.
            feature proposals) that must never auto-deploy regardless of level.

    Returns:
        A :class:`GateDecision`.
    """
    if not safety_result.get("passed", False):
        return GateDecision.halt

    # L1 is suggest-only: a passing safety check still never deploys.
    if autonomy_level <= 1:
        return GateDecision.halt

    if always_require_approval:
        return GateDecision.approve

    # L4 is full autonomy (the documented contract): any action that passed
    # safety deploys. Without this branch L4 behaved identically to L2/L3,
    # routing everything non-low-risk to approval.
    if autonomy_level >= 4:
        return GateDecision.deploy

    risk = safety_result.get("risk_level", "high")
    if autonomy_level >= 3 and risk == "low":
        return GateDecision.deploy

    if autonomy_level >= 2:
        return GateDecision.approve

    return GateDecision.halt
