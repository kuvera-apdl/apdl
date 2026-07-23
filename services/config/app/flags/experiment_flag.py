"""Derive an experiment's canonical backing feature flag.

An experiment is *measured through a flag's exposures*: the SDK buckets users by
evaluating a flag and the Query service analyses results by ``flag_key``. So
creating or editing an experiment must initialize/sync a flag whose key,
variants, rollout, targeting and serving state match the experiment.

This module is the single experiment→flag mapping. All callers (REST API, admin
console, agent) go through the Config admin endpoints, so no other service builds
the flag itself — that is what keeps the two from drifting.
"""

from __future__ import annotations

from app.models.schemas import (
    FallthroughConfig,
    ExperimentTargetingRule,
    FlagCreate,
    GateRule,
    RolloutConfig,
    VariantConfig,
)

# Bucket key for the synthesized fallthrough rollout — the system-wide flag
# default (see the flags DDL and DEFAULT_FALLTHROUGH).
DEFAULT_BUCKET_BY = "user_id"

# Experiment status → (flag state, enabled). This is the single place an
# experiment's lifecycle drives flag serving. ``validate_state_enabled`` (run by
# FlagCreate/FlagUpdate) enforces ``enabled == (state == "active")``, so these
# pairs must stay consistent.
_STATUS_TO_FLAG_STATE: dict[str, tuple[str, bool]] = {
    "draft": ("draft", False),
    "scheduled": ("draft", False),
    "running": ("active", True),
    "completed": ("disabled", False),
    "stopped": ("disabled", False),
}


def status_to_flag_state(status: str) -> tuple[str, bool]:
    """Map an experiment status to its ``(flag_state, enabled)`` pair."""
    return _STATUS_TO_FLAG_STATE[status]


def _flag_fields(
    *,
    flag_key: str,
    name: str,
    description: str,
    status: str,
    variants: list[VariantConfig],
    default_variant: str,
    traffic_percentage: float,
    targeting_rules: list[ExperimentTargetingRule],
    bucket_by: str,
) -> dict:
    """Derived flag fields shared by the create and update projections.

    Experiment targeting rules express eligibility only.  The single global
    ``traffic_percentage`` is projected onto every eligible rule.  When rules
    exist, a zero-percent fallthrough excludes actors that match none of them;
    otherwise the fallthrough enrolls the global traffic population.
    """
    state, enabled = status_to_flag_state(status)
    return {
        "name": name or flag_key,
        "description": description,
        "state": state,
        "enabled": enabled,
        "default_variant": default_variant,
        "variants": variants,
        "rules": [
            GateRule(
                id=rule.id,
                name=rule.name,
                conditions=rule.conditions,
                rollout=RolloutConfig(
                    percentage=traffic_percentage,
                    bucket_by=bucket_by,
                ),
            )
            for rule in targeting_rules
        ],
        "fallthrough": FallthroughConfig(
            rollout=RolloutConfig(
                percentage=0.0 if targeting_rules else traffic_percentage,
                bucket_by=bucket_by,
            ),
        ),
        "evaluation_mode": "client",
        "auto_disable": False,
    }


def build_flag_create(
    *,
    flag_key: str,
    name: str,
    description: str,
    status: str,
    variants: list[VariantConfig],
    default_variant: str,
    traffic_percentage: float,
    targeting_rules: list[ExperimentTargetingRule],
    bucket_by: str = DEFAULT_BUCKET_BY,
) -> FlagCreate:
    """Project an experiment onto a ``FlagCreate`` (validated by the flag model)."""
    return FlagCreate(
        key=flag_key,
        **_flag_fields(
            flag_key=flag_key,
            name=name,
            description=description,
            status=status,
            variants=variants,
            default_variant=default_variant,
            traffic_percentage=traffic_percentage,
            targeting_rules=targeting_rules,
            bucket_by=bucket_by,
        ),
    )


def build_flag_projection(
    *,
    flag_key: str,
    name: str,
    description: str,
    status: str,
    variants: list[VariantConfig],
    default_variant: str,
    traffic_percentage: float,
    targeting_rules: list[ExperimentTargetingRule],
    bucket_by: str = DEFAULT_BUCKET_BY,
) -> dict:
    """Return lifecycle-owned fields for an experiment backing flag.

    The public ``FlagUpdate`` request intentionally excludes lifecycle fields;
    only the atomic experiment command applies this internal projection.
    """
    projection = FlagCreate(
        key=flag_key,
        **_flag_fields(
            flag_key=flag_key,
            name=name,
            description=description,
            status=status,
            variants=variants,
            default_variant=default_variant,
            traffic_percentage=traffic_percentage,
            targeting_rules=targeting_rules,
            bucket_by=bucket_by,
        ),
    )
    return projection.model_dump(
        mode="json",
        exclude={"key", "owners", "review_by", "guardrails"},
    )
