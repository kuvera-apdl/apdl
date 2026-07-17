"""Canonical envelope for agent actions.

Agent actions live in two places by design:
  * The mutable approval lifecycle (pending -> approved/rejected -> executed)
    stays in PostgreSQL `agent_audit_log`. That's where humans interact.
  * Once an action is `auto` or `approved`, a mirror envelope is published
    to the `decisions:raw:{project_id}` Redis Stream and lands in ClickHouse
    `decisions_v2`. That's what agents and analysts query.

LLM calls use the strict runtime contracts in ``app.llm.contracts`` and the
PostgreSQL logical-call/provider-attempt ledgers; they are not action-stream
envelopes.
"""

import hashlib
import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ---------- agent action payload ----------

ActionType = Literal[
    "propose_experiment",
    "deploy_experiment",
    "personalize_slot",
    "propose_feature",
    "modify_flag_rollout",
    "send_insight",
    "open_pull_request",
]

ApprovalStatus = Literal[
    "auto", "pending", "approved", "rejected", "executed", "rolled_back"
]


class AgentActionPayload(BaseModel):
    """One agent-proposed or agent-executed action."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    agent_type: str = Field(min_length=1, max_length=64)
    action_type: ActionType
    approval_status: ApprovalStatus = "pending"

    # The structured config of the action — what the action *does*.
    # Shape varies per action_type; agents that consume this should switch
    # on action_type. Kept as a free-form dict here because the validator
    # in services/agents/app/safety/ already enforces per-action schemas.
    config: dict[str, Any] = Field(default_factory=dict)

    # Output of the safety validator — pass/fail + reasons.
    safety_result: dict[str, Any] = Field(default_factory=dict)

    # Optional: link to the human approval row when applicable.
    audit_log_id: UUID | None = None


# ---------- envelope ----------

_AgentSchema = Literal["agent_action@1"]


class AgentEnvelope(BaseModel):
    """Canonical envelope for agent-produced records."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: UUID = Field(alias="_id")
    schema_: _AgentSchema = Field(alias="_schema")
    project_id: str = Field(
        alias="_project_id",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9]+$",
    )
    idempotency_key: str = Field(alias="_idempotency_key", min_length=1, max_length=128)
    correlation_id: UUID | None = Field(default=None, alias="_correlation_id")
    source: str = Field(alias="_source", min_length=1, max_length=64)
    occurred_at: datetime = Field(alias="_occurred_at")

    # Payload shape is fixed by the envelope's sole canonical schema.
    payload: AgentActionPayload


# ---------- idempotency helpers ----------


def agent_action_idempotency_key(
    run_id: UUID, action_type: str, config: dict[str, Any]
) -> str:
    """A deterministic key per (run, action_type, config). Re-emitting the
    same action — for instance after a retry — collapses to one row."""
    config_blob = json.dumps(config, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(config_blob.encode("utf-8")).hexdigest()[:32]
    return f"act:{run_id}:{action_type}:{digest}"
