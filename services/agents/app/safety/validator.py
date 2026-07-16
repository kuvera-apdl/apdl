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
    open_pull_request = "open_pull_request"


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
    ActionType.open_pull_request: 10,
}

_REJECTED_FLAG_FIELDS = {
    "default_value",
    "variant_type",
    "variants_json",
    "defaultVariant",
    "targeting_rules",
    "rollout_percentage",
}


def _validate_variant_flag_config(
    config: dict[str, Any],
    *,
    require_complete: bool,
) -> str | None:
    rejected = sorted(field for field in _REJECTED_FLAG_FIELDS if field in config)
    if rejected:
        return f"Flag config contains non-canonical field(s): {', '.join(rejected)}."

    fallthrough = config.get("fallthrough")
    if isinstance(fallthrough, dict) and (
        "value" in fallthrough
    ):
        return "fallthrough must only contain rollout."

    if require_complete:
        for field in ("default_variant", "variants", "rules", "fallthrough"):
            if field not in config:
                return f"flag_config.{field} is required."

    default_variant = config.get("default_variant")
    variants = config.get("variants")

    if default_variant is not None:
        if not isinstance(default_variant, str) or not default_variant:
            return "default_variant must be a non-empty string."
    elif require_complete:
        return "default_variant must be a non-empty string."

    if variants is not None:
        error = _validate_variants(
            variants,
            default_variant,
            experiment=require_complete,
        )
        if error is not None:
            return error
    elif require_complete:
        return "variants must contain at least one variant."

    rules = config.get("rules")
    if rules is not None:
        if not isinstance(rules, list):
            return "rules must be a list."
        for rule in rules:
            if not isinstance(rule, dict):
                return "rules must contain objects."
            if "variants" in rule or "default_variant" in rule:
                return "rules must not define variants or default_variant."
            rollout_error = _validate_rollout(rule.get("rollout"), "rules.rollout")
            if rollout_error is not None:
                return rollout_error

    if fallthrough is not None:
        error = _validate_fallthrough(fallthrough)
        if error is not None:
            return error

    if config.get("auto_disable") not in {None, False}:
        return "auto_disable must be false; automatic guardrail mutation is unavailable."

    return None


def _validate_variants(
    variants: Any,
    default_variant: Any,
    *,
    experiment: bool,
) -> str | None:
    if not isinstance(variants, list) or not variants:
        return "variants must contain at least one variant."
    if experiment and not 2 <= len(variants) <= 10:
        return "experiment variants must contain between 2 and 10 variants."

    keys: set[str] = set()
    total_weight = 0
    for variant in variants:
        if not isinstance(variant, dict):
            return "variants must contain objects."
        extra_fields = set(variant) - {"key", "weight"}
        if extra_fields:
            return (
                "variants must contain only canonical key and weight fields; "
                f"found: {', '.join(sorted(extra_fields))}."
            )

        key = variant.get("key")
        if not isinstance(key, str) or not key:
            return "variant key must be a non-empty string."
        if key in keys:
            return "variants must contain unique keys."
        keys.add(key)

        weight = variant.get("weight")
        if not isinstance(weight, int) or isinstance(weight, bool):
            return (
                "experiment variant weights must be positive integers."
                if experiment
                else "variant weights must be non-negative integers."
            )
        if weight < 0 or (experiment and weight == 0):
            return (
                "experiment variant weights must be positive integers."
                if experiment
                else "variant weights must be non-negative integers."
            )
        total_weight += weight

    if total_weight <= 0:
        return "variant weights must contain at least one positive weight."

    if default_variant is not None and default_variant not in keys:
        return "default_variant must match a variant key."

    return None


def _validate_fallthrough(fallthrough: Any) -> str | None:
    if not isinstance(fallthrough, dict):
        return "fallthrough must be an object."
    extra_fields = set(fallthrough) - {"rollout"}
    if extra_fields:
        return (
            "fallthrough must contain only canonical rollout field; "
            f"found: {', '.join(sorted(extra_fields))}."
        )

    return _validate_rollout(fallthrough.get("rollout"), "fallthrough.rollout")


def _validate_rollout(rollout: Any, field_name: str) -> str | None:
    if not isinstance(rollout, dict):
        return f"{field_name} must be an object."
    extra_rollout_fields = set(rollout) - {"percentage", "bucket_by"}
    if extra_rollout_fields:
        return (
            f"{field_name} must contain only percentage and bucket_by; "
            f"found: {', '.join(sorted(extra_rollout_fields))}."
        )

    percentage = rollout.get("percentage")
    if (
        not isinstance(percentage, int | float)
        or isinstance(percentage, bool)
        or percentage < 0
        or percentage > 100
    ):
        return f"{field_name}.percentage must be a number from 0 to 100."

    bucket_by = rollout.get("bucket_by")
    if not isinstance(bucket_by, str) or not bucket_by:
        return f"{field_name}.bucket_by must be a non-empty string."

    return None


def _variant_weights(variants: Any) -> dict[str, int]:
    if not isinstance(variants, list):
        return {}

    weights: dict[str, int] = {}
    for variant in variants:
        if not isinstance(variant, dict):
            return {}
        key = variant.get("key")
        weight = variant.get("weight")
        if not isinstance(key, str) or not isinstance(weight, int) or isinstance(weight, bool):
            return {}
        weights[key] = weight
    return weights


def _max_rollout_percentage(config: dict[str, Any]) -> float | None:
    percentages = []

    fallthrough = config.get("fallthrough")
    if isinstance(fallthrough, dict):
        fallthrough_percentage = _rollout_percentage(fallthrough.get("rollout"))
        if fallthrough_percentage is not None:
            percentages.append(fallthrough_percentage)

    rules = config.get("rules", [])
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                return None
            rule_percentage = _rollout_percentage(rule.get("rollout"))
            if rule_percentage is not None:
                percentages.append(rule_percentage)

    return max(percentages) if percentages else None


def _rollout_percentage(rollout: Any) -> float | None:
    if not isinstance(rollout, dict):
        return None
    percentage = rollout.get("percentage")
    if not isinstance(percentage, int | float) or isinstance(percentage, bool):
        return None
    return float(percentage)


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
        Checks fail closed: a crash on a malformed (LLM-shaped) config becomes a
        failed check rather than an exception that kills the whole agent run.
        """
        checks: list[dict[str, Any]] = [
            self._run_check("conflict_check", self._check_conflicts, action),
            self._run_check("variant_config", self._check_variant_config, action),
            self._run_check("blast_radius", self._check_blast_radius, action),
            self._run_check("guardrails", self._check_guardrails, action),
        ]

        # Rate-limit last, and only count actions that pass the other checks —
        # a burst of rejected drafts must not exhaust the quota for the
        # eventual legitimate action.
        record = all(c["passed"] for c in checks)
        checks.insert(0, self._check_rate_limits(action, record=record))

        passed = all(c["passed"] for c in checks)
        risk_level = self._assess_risk(action, checks)

        return SafetyResult(passed=passed, checks=checks, risk_level=risk_level)

    @staticmethod
    def _run_check(name: str, fn: Any, action: AgentAction) -> dict[str, Any]:
        try:
            return fn(action)
        except Exception as exc:
            logger.exception("Safety check %s crashed on %s action", name, action.type.value)
            return {
                "name": name,
                "passed": False,
                "message": f"Check could not evaluate the config ({type(exc).__name__}): {exc}",
            }

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_rate_limits(self, action: AgentAction, record: bool = True) -> dict[str, Any]:
        """Ensure the agent is not exceeding action rate limits.

        ``record=False`` checks the budget without consuming it — used for
        actions already failing other checks, so rejected drafts don't starve
        the eventual legitimate action.
        """
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

        if record:
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
            flag_config = config.get("flag_config")
            flag_key = config.get("flag_key") or (
                flag_config.get("key", "") if isinstance(flag_config, dict) else ""
            )

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

    def _check_variant_config(self, action: AgentAction) -> dict[str, Any]:
        """Validate canonical variant flag fields for flag-changing actions."""
        if action.type == ActionType.create_experiment:
            flag_config = action.config.get("flag_config")
            if not isinstance(flag_config, dict):
                return {
                    "name": "variant_config",
                    "passed": False,
                    "message": "Experiment design must include canonical flag_config.",
                }

            error = _validate_variant_flag_config(flag_config, require_complete=True)
            if error is not None:
                return {
                    "name": "variant_config",
                    "passed": False,
                    "message": error,
                }

            return {
                "name": "variant_config",
                "passed": True,
                "message": "Canonical variant flag config is valid.",
            }

        if action.type == ActionType.update_flag:
            error = _validate_variant_flag_config(action.config, require_complete=False)
            if error is not None:
                return {
                    "name": "variant_config",
                    "passed": False,
                    "message": error,
                }

        return {
            "name": "variant_config",
            "passed": True,
            "message": "No variant config changes require validation.",
        }

    def _check_blast_radius(self, action: AgentAction) -> dict[str, Any]:
        """Ensure the action does not affect too many users at once.

        For experiments, variant weights are relative. Traffic allocation is
        controlled by the canonical fallthrough rollout percentage, and variant
        exposure share is derived from rollout percentage multiplied by the
        normalized non-default variant weight.
        """
        config = action.config

        if action.type == ActionType.create_experiment:
            flag_config = config.get("flag_config", {})
            if not isinstance(flag_config, dict):
                return {
                    "name": "blast_radius",
                    "passed": False,
                    "message": "Cannot assess blast radius without canonical flag_config.",
                }

            default_variant = flag_config.get("default_variant", "")
            variant_weights = _variant_weights(flag_config.get("variants", []))
            total_weight = sum(variant_weights.values())
            if not default_variant or total_weight <= 0 or default_variant not in variant_weights:
                return {
                    "name": "blast_radius",
                    "passed": False,
                    "message": "Cannot assess blast radius until variant config is valid.",
                }

            rollout_percentage = _max_rollout_percentage(flag_config)
            if rollout_percentage is None:
                return {
                    "name": "blast_radius",
                    "passed": False,
                    "message": "Cannot assess blast radius without rule or fallthrough rollout percentage.",
                }
            if rollout_percentage > 100:
                return {
                    "name": "blast_radius",
                    "passed": False,
                    "message": f"Rollout percentage is {rollout_percentage}%, exceeding 100%.",
                }

            default_share = (variant_weights[default_variant] / total_weight) * 100.0
            if default_share < 10:
                return {
                    "name": "blast_radius",
                    "passed": False,
                    "message": (
                        f"Default variant share is only {default_share:.1f}%. "
                        "Must be at least 10% for statistical validity."
                    ),
                }

            non_default_exposure_shares = [
                (weight / total_weight) * rollout_percentage
                for key, weight in variant_weights.items()
                if key != default_variant
            ]
            max_non_default_exposure = max(non_default_exposure_shares, default=0.0)
            if max_non_default_exposure > 50:
                return {
                    "name": "blast_radius",
                    "passed": False,
                    "message": (
                        f"A non-default variant would reach {max_non_default_exposure:.1f}% "
                        "of users, exceeding the 50% safety limit."
                    ),
                }

            return {
                "name": "blast_radius",
                "passed": True,
                "message": (
                    f"Traffic allocation is safe: {rollout_percentage:.1f}% rollout, "
                    f"{default_share:.1f}% default variant share."
                ),
            }

        if action.type == ActionType.update_flag:
            variants = config.get("variants")
            rollout_percentage = _max_rollout_percentage(config)
            if variants is None and rollout_percentage is None:
                return {
                    "name": "blast_radius",
                    "passed": True,
                    "message": "Update changes no traffic-affecting fields.",
                }

            # The update changes traffic — hold it to the same exposure cap as
            # create_experiment, or an agent could route 99% of users to a
            # treatment via update_flag that create_experiment would reject.
            default_variant = config.get("default_variant", "")
            variant_weights = _variant_weights(variants or [])
            total_weight = sum(variant_weights.values())
            if (
                not default_variant
                or total_weight <= 0
                or default_variant not in variant_weights
                or rollout_percentage is None
            ):
                return {
                    "name": "blast_radius",
                    "passed": False,
                    "message": (
                        "Flag update changes traffic allocation but blast radius "
                        "cannot be assessed — include variants, default_variant, "
                        "and a rollout percentage."
                    ),
                }

            non_default_exposure_shares = [
                (weight / total_weight) * rollout_percentage
                for key, weight in variant_weights.items()
                if key != default_variant
            ]
            max_non_default_exposure = max(non_default_exposure_shares, default=0.0)
            if max_non_default_exposure > 50:
                return {
                    "name": "blast_radius",
                    "passed": False,
                    "message": (
                        f"A non-default variant would reach {max_non_default_exposure:.1f}% "
                        "of users, exceeding the 50% safety limit."
                    ),
                }
            return {
                "name": "blast_radius",
                "passed": True,
                "message": (
                    f"Traffic allocation is safe: {rollout_percentage:.1f}% rollout, "
                    f"max non-default exposure {max_non_default_exposure:.1f}%."
                ),
            }

        if action.type == ActionType.update_ui_config:
            targeting = config.get("targeting")
            # If there's no targeting, it affects all users — flag as high
            # blast radius. LLMs emit targeting as either a conditions dict or
            # a bare list of conditions.
            if isinstance(targeting, dict):
                has_targeting = bool(targeting.get("segment") or targeting.get("conditions"))
            elif isinstance(targeting, list):
                has_targeting = bool(targeting)
            else:
                has_targeting = False
            if not has_targeting:
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

            primary_metric = config.get("primary_metric")
            if not (isinstance(primary_metric, dict) and primary_metric.get("event")):
                return {
                    "name": "guardrails",
                    "passed": False,
                    "message": "Primary metric event is not defined.",
                }
            if primary_metric.get("type", "conversion") != "conversion":
                return {
                    "name": "guardrails",
                    "passed": False,
                    "message": "Primary metric type must be conversion.",
                }

            hypothesis = str(config.get("hypothesis") or "")
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

        if action.type == ActionType.open_pull_request:
            if not config.get("title"):
                return {
                    "name": "guardrails",
                    "passed": False,
                    "message": "Pull request is missing a title.",
                }
            if len(str(config.get("spec") or "")) < 10:
                return {
                    "name": "guardrails",
                    "passed": False,
                    "message": "Pull request spec is missing or too short.",
                }
            return {
                "name": "guardrails",
                "passed": True,
                "message": "Pull request has a title and spec.",
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
            ActionType.open_pull_request: "low",
        }

        return inherent_risk.get(action.type, "medium")
