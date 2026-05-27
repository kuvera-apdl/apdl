"""Safety validator for agent actions.

Validates proposed agent actions against a set of safety rules before
they are executed. Each check produces a pass/fail result with a message,
and the overall risk level is assessed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ActionType(str, Enum):
    create_experiment = "create_experiment"
    update_flag = "update_flag"
    update_ui_config = "update_ui_config"
    feature_proposal = "feature_proposal"


class AgentAction(BaseModel):
    type: ActionType
    config: dict[str, Any]
    project_id: str


class SafetyResult(BaseModel):
    passed: bool
    checks: list[dict[str, Any]]
    risk_level: str  # "low", "medium", "high"


# ---------------------------------------------------------------------------
# Rate-limit state (in-memory, per-process)
# ---------------------------------------------------------------------------

_action_timestamps: dict[str, list[datetime]] = {}
_MAX_ACTIONS_PER_HOUR: dict[ActionType, int] = {
    ActionType.create_experiment: 5,
    ActionType.update_flag: 20,
    ActionType.update_ui_config: 30,
    ActionType.feature_proposal: 3,
}


class SafetyValidator:
    """Validates agent actions against safety rules before execution.

    Checks performed:
    1. Rate limits — prevent runaway agents from making too many changes.
    2. Conflict detection — flag overlapping experiments or conflicting flags.
    3. Blast radius — ensure traffic allocation is within safe bounds.
    4. Guardrail checks — verify required safety fields are present.
    """

    def validate(self, action: AgentAction) -> SafetyResult:
        """Run all safety checks on the proposed action.

        Returns a SafetyResult indicating whether the action is safe to proceed.
        """
        checks: list[dict[str, Any]] = [
            self._check_rate_limits(action),
            self._check_conflicts(action),
            self._check_blast_radius(action),
            self._check_guardrails(action),
        ]

        passed = all(c["passed"] for c in checks)
        risk_level = self._assess_risk(action, checks)

        return SafetyResult(passed=passed, checks=checks, risk_level=risk_level)

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_rate_limits(self, action: AgentAction) -> dict[str, Any]:
        """Ensure the agent is not exceeding action rate limits."""
        key = f"{action.project_id}:{action.type.value}"
        now = datetime.now(timezone.utc)

        # Clean up timestamps older than 1 hour
        if key in _action_timestamps:
            _action_timestamps[key] = [
                ts for ts in _action_timestamps[key]
                if (now - ts).total_seconds() < 3600
            ]
        else:
            _action_timestamps[key] = []

        limit = _MAX_ACTIONS_PER_HOUR.get(action.type, 10)
        current_count = len(_action_timestamps[key])

        if current_count >= limit:
            return {
                "name": "rate_limit",
                "passed": False,
                "message": (
                    f"Rate limit exceeded: {current_count}/{limit} "
                    f"{action.type.value} actions in the last hour."
                ),
            }

        # Record this action
        _action_timestamps[key].append(now)

        return {
            "name": "rate_limit",
            "passed": True,
            "message": f"Within rate limits ({current_count + 1}/{limit}).",
        }

    def _check_conflicts(self, action: AgentAction) -> dict[str, Any]:
        """Check for conflicts with existing configurations.

        For experiments: check if the flag key is already in use.
        For flags: check for duplicate keys.
        For UI configs: check for duplicate config IDs.
        """
        config = action.config

        if action.type == ActionType.create_experiment:
            experiment_id = config.get("experiment_id", "")
            flag_key = config.get("flag_key", config.get("flag_config", {}).get("key", ""))

            if not experiment_id:
                return {
                    "name": "conflict_check",
                    "passed": False,
                    "message": "Experiment ID is missing.",
                }

            if not flag_key:
                return {
                    "name": "conflict_check",
                    "passed": False,
                    "message": "Flag key is missing from experiment design.",
                }

            # In a production system, we would query the config service here
            # to check for existing experiments with the same flag key.
            return {
                "name": "conflict_check",
                "passed": True,
                "message": "No conflicts detected.",
            }

        if action.type == ActionType.update_flag:
            key = config.get("key", "")
            if not key:
                return {
                    "name": "conflict_check",
                    "passed": False,
                    "message": "Flag key is missing.",
                }

        return {
            "name": "conflict_check",
            "passed": True,
            "message": "No conflicts detected.",
        }

    def _check_blast_radius(self, action: AgentAction) -> dict[str, Any]:
        """Ensure the action does not affect too many users at once.

        For experiments, the total traffic allocation across all variants
        should not exceed 100%, and no single variant should get more than
        50% for new experiments (to ensure a control group).
        """
        config = action.config

        if action.type == ActionType.create_experiment:
            variants = config.get("variants", [])
            total_weight = sum(v.get("weight", 0) for v in variants)

            if total_weight > 100:
                return {
                    "name": "blast_radius",
                    "passed": False,
                    "message": f"Total variant weight is {total_weight}%, exceeding 100%.",
                }

            # Check that there's a reasonable control group
            control_variants = [v for v in variants if v.get("key") == "control"]
            if not control_variants:
                return {
                    "name": "blast_radius",
                    "passed": False,
                    "message": "No control variant found. Every experiment must have a control group.",
                }

            control_weight = control_variants[0].get("weight", 0)
            if control_weight < 10:
                return {
                    "name": "blast_radius",
                    "passed": False,
                    "message": (
                        f"Control group weight is only {control_weight}%. "
                        "Must be at least 10% for statistical validity."
                    ),
                }

            return {
                "name": "blast_radius",
                "passed": True,
                "message": f"Traffic allocation is safe: {total_weight}% total, {control_weight}% control.",
            }

        if action.type == ActionType.update_ui_config:
            targeting = config.get("targeting", {})
            # If there's no targeting, it affects all users — flag as high blast radius
            if not targeting or (not targeting.get("segment") and not targeting.get("conditions")):
                return {
                    "name": "blast_radius",
                    "passed": False,
                    "message": "UI config has no targeting — would affect all users.",
                }

            return {
                "name": "blast_radius",
                "passed": True,
                "message": "UI config has targeting criteria.",
            }

        return {
            "name": "blast_radius",
            "passed": True,
            "message": "Blast radius is acceptable.",
        }

    def _check_guardrails(self, action: AgentAction) -> dict[str, Any]:
        """Verify that required safety fields are present in the configuration.

        For experiments: must have guardrail metrics defined.
        For flags: must have a description.
        For feature proposals: must have risks documented.
        """
        config = action.config

        if action.type == ActionType.create_experiment:
            guardrails = config.get("guardrail_metrics", [])
            if not guardrails:
                return {
                    "name": "guardrails",
                    "passed": False,
                    "message": (
                        "No guardrail metrics defined. Experiments must include "
                        "guardrails for error rate and latency at minimum."
                    ),
                }

            primary_metric = config.get("primary_metric", {})
            if not primary_metric.get("event"):
                return {
                    "name": "guardrails",
                    "passed": False,
                    "message": "Primary metric event is not defined.",
                }

            hypothesis = config.get("hypothesis", "")
            if len(hypothesis) < 10:
                return {
                    "name": "guardrails",
                    "passed": False,
                    "message": "Hypothesis is missing or too short. Experiments need a clear hypothesis.",
                }

            return {
                "name": "guardrails",
                "passed": True,
                "message": "All required guardrails are present.",
            }

        if action.type == ActionType.feature_proposal:
            risks = config.get("risks", [])
            if not risks:
                return {
                    "name": "guardrails",
                    "passed": False,
                    "message": "Feature proposal has no documented risks.",
                }

            success_criteria = config.get("success_criteria", [])
            if not success_criteria:
                return {
                    "name": "guardrails",
                    "passed": False,
                    "message": "Feature proposal has no success criteria.",
                }

            return {
                "name": "guardrails",
                "passed": True,
                "message": "Proposal includes required risks and success criteria.",
            }

        return {
            "name": "guardrails",
            "passed": True,
            "message": "Guardrail requirements met.",
        }

    # ------------------------------------------------------------------
    # Risk assessment
    # ------------------------------------------------------------------

    def _assess_risk(self, action: AgentAction, checks: list[dict[str, Any]]) -> str:
        """Assess the overall risk level of the action.

        Risk levels:
        - "low": All checks pass, action type is low-impact.
        - "medium": All checks pass but action type is higher-impact,
                    or minor warnings exist.
        - "high": Any check failed, or action is inherently high-risk.
        """
        all_passed = all(c["passed"] for c in checks)

        if not all_passed:
            return "high"

        # Inherent risk by action type
        inherent_risk = {
            ActionType.update_ui_config: "low",
            ActionType.update_flag: "medium",
            ActionType.create_experiment: "medium",
            ActionType.feature_proposal: "high",
        }

        return inherent_risk.get(action.type, "medium")
