"""Decision transforms -> ``decisions_v2``.

A "decision" is anything APDL produces rather than the user: a flag evaluation,
an experiment exposure, an agent action, a personalization choice. All four
share the unified ``decisions_v2`` envelope with sparse, per-schema promoted
columns — so they share one base that lays down the full column set with
defaults, and each concrete type overrides :meth:`promoted` to fill in only the
columns that apply to it. This keeps the INSERT column list identical across
schemas (required for a uniform batch insert) while each row stays meaningful.
"""

from __future__ import annotations

from typing import Any

from etl.base import BaseTransform, _json
from etl.context import ZERO_UUID, EtlContext, Row
from etl.envelope import CanonicalEnvelope
from etl.registry import register_transform

DECISIONS_V2_COLUMNS = (
    "_id", "_schema", "_project_id", "_idempotency_key", "_correlation_id",
    "_source", "_occurred_at", "_received_at",
    "user_id", "anonymous_id", "session_id",
    "flag_key", "experiment_key", "variant", "reason", "rule_id",
    "rollout_bucket", "action_type", "approval_status", "run_id",
    "payload", "safety_result",
)


class _DecisionTransform(BaseTransform):
    """Shared mapping for every decision going to ``decisions_v2``."""

    target_table = "decisions_v2"
    columns = DECISIONS_V2_COLUMNS

    def promoted(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Per-schema sparse columns to overlay on the defaults."""
        return {}

    def build_row(
        self, env: CanonicalEnvelope, ctx: EtlContext, enrichment: dict[str, Any]
    ) -> Row:
        p = env.payload
        row = self.envelope_columns(env, ctx)
        row.update(
            {
                "user_id": p.get("user_id") or "",
                "anonymous_id": p.get("anonymous_id", ""),
                "session_id": p.get("session_id", ""),
                # sparse promoted columns — defaults match the DDL defaults
                "flag_key": "",
                "experiment_key": "",
                "variant": "",
                "reason": "",
                "rule_id": "",
                "rollout_bucket": 0,
                "action_type": "",
                "approval_status": "",
                "run_id": ZERO_UUID,
                "payload": _json(p),
                "safety_result": "",
            }
        )
        row.update(self.promoted(p))
        return row


@register_transform
class FlagEvalTransform(_DecisionTransform):
    """Config service evaluated a flag for a user."""

    schema = "flag_eval@1"
    description = "Flag evaluation (flag_eval@1) -> decisions_v2."

    def promoted(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "flag_key": payload.get("flag_key", ""),
            "variant": payload.get("variant", ""),
            "reason": payload.get("reason", ""),
            "rule_id": payload.get("rule_id", ""),
            "rollout_bucket": int(payload.get("rollout_bucket", 0)),
        }


@register_transform
class ExposureTransform(_DecisionTransform):
    """A user was assigned a variant in a running experiment."""

    schema = "exposure@1"
    description = "Experiment exposure (exposure@1) -> decisions_v2."

    def promoted(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "flag_key": payload.get("flag_key", ""),
            "experiment_key": payload.get("experiment_key", ""),
            "variant": payload.get("variant", ""),
            "rollout_bucket": int(payload.get("rollout_bucket", 0)),
        }


@register_transform
class AgentActionTransform(_DecisionTransform):
    """The agents service proposed or executed an action."""

    schema = "agent_action@1"
    description = "Agent action (agent_action@1) -> decisions_v2."

    def promoted(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = payload.get("run_id") or ZERO_UUID
        safety = payload.get("safety_result")
        return {
            "experiment_key": payload.get("experiment_key", ""),
            "action_type": payload.get("action_type", ""),
            "approval_status": payload.get("approval_status", ""),
            "run_id": str(run_id),
            "safety_result": _json(safety) if safety else "",
        }


@register_transform
class PersonalizationTransform(_DecisionTransform):
    """A runtime selection of a UI/content variant for a user."""

    schema = "personalization@1"
    description = "Personalization choice (personalization@1) -> decisions_v2."

    def promoted(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "experiment_key": payload.get("experiment_key", ""),
            "variant": payload.get("variant", ""),
        }
