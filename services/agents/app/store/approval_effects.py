"""Durable approval commands and their retryable mutation effects.

The approval HTTP boundary is deliberately database-only.  It locks the exact
persisted gate, records all human decisions and mandatory audit intents, and
queues ordered effects in one transaction.  A replica-safe worker leases those
effects and performs Config/Codegen calls with the persisted idempotency key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import asyncpg
import httpx

from app.graphs.experiment_design import (
    open_treatment_changeset,
    stage_experiment_draft,
    treatment_changeset_task,
)
from app.readiness import CodegenChangesetCapability
from app.store.experiments import record_designed_experiment
from app.store.mutation_quotas import (
    MutationQuotaExceededError,
    reserve_mutation,
)
from app.store.proposals import (
    enqueue_proposals,
    get_proposal,
    mark_failed,
    mark_implemented,
)
from app.tools.code import open_changeset

logger = logging.getLogger(__name__)

EFFECT_LEASE_SECONDS = 5 * 60
EFFECT_POLL_SECONDS = 1.0

GateAgent = Literal["experiment_design", "feature_proposal", "code_implementation"]
EffectType = Literal[
    "stage_experiment_draft",
    "open_treatment_changeset",
    "open_code_changeset",
    "record_experiment_rejection",
    "record_proposal_rejection",
    "quarantine_feature_proposal",
]

_GATE_RESULT_KEY: dict[str, str] = {
    "experiment_design": "experiment_designs",
    "feature_proposal": "feature_proposals",
    "code_implementation": "changesets",
}
_CODEGEN_EFFECT_TYPES = frozenset({"open_treatment_changeset", "open_code_changeset"})


class ApprovalCommandError(RuntimeError):
    """Base error for an approval command that was not enqueued."""


class ApprovalRunNotFoundError(ApprovalCommandError):
    """The tenant-scoped run does not exist."""


class ApprovalGateConflictError(ApprovalCommandError):
    """The run no longer owns the gate represented by the request."""


class ApprovalDecisionError(ApprovalCommandError):
    """Decisions do not exactly cover the persisted canonical gate items."""


class ApprovalCapabilityError(ApprovalCommandError):
    """An approved item requires a capability the deployment cannot provide."""

    def __init__(self, capability: CodegenChangesetCapability) -> None:
        self.capability = capability
        super().__init__(
            "Codegen changeset creation is "
            f"{capability}; reject code-backed items or enable a publication stage."
        )


class RetryableApprovalEffectError(RuntimeError):
    def __init__(self, message: str, *, delay_seconds: int | None = None) -> None:
        self.delay_seconds = delay_seconds
        super().__init__(message)


class PermanentApprovalEffectError(RuntimeError):
    """An effect cannot become valid by retrying the same persisted payload."""


@dataclass(frozen=True)
class ApprovalDecision:
    item_id: str
    approved: bool


@dataclass(frozen=True)
class ApprovalEffectView:
    effect_id: str
    item_id: str
    effect_type: str
    status: str
    attempt_count: int
    last_error: str | None
    result: dict[str, Any] | None


@dataclass(frozen=True)
class ApprovalCommandView:
    command_id: str
    run_id: str
    actor_credential_id: str
    actor_user_id: str | None
    gate_id: str
    gate_agent: str
    status: str
    approved_count: int
    rejected_count: int
    comment: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    effects: tuple[ApprovalEffectView, ...]


@dataclass(frozen=True)
class _PlannedEffect:
    effect_id: uuid.UUID
    item_id: str
    effect_type: EffectType
    effect_order: int
    depends_on_effect_id: uuid.UUID | None
    payload: dict[str, Any]
    quota_action_type: str | None


@dataclass(frozen=True)
class _ClaimedEffect:
    effect_id: str
    command_id: str
    run_id: str
    project_id: str
    item_id: str
    effect_type: str
    payload: Any
    idempotency_key: str
    quota_action_type: str | None
    attempt_count: int
    max_attempts: int
    lease_owner_id: str


def _json_object(raw: Any, *, label: str) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ApprovalGateConflictError(f"Persisted {label} is malformed") from exc
    if not isinstance(raw, dict):
        raise ApprovalGateConflictError(f"Persisted {label} must be an object")
    return dict(raw)


def _json_array(raw: Any, *, label: str) -> list[Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ApprovalGateConflictError(f"Persisted {label} is malformed") from exc
    if not isinstance(raw, list):
        raise ApprovalGateConflictError(f"Persisted {label} must be an array")
    return list(raw)


def _canonical_item_id(gate_agent: str, item: dict[str, Any]) -> str:
    field = "experiment_id" if gate_agent == "experiment_design" else "proposal_id"
    value = item.get(field)
    if not isinstance(value, str):
        raise ApprovalGateConflictError(
            f"Persisted {gate_agent} gate item requires string {field}"
        )
    if not value or value != value.strip() or len(value) > 128:
        raise ApprovalGateConflictError(
            f"Persisted {gate_agent} gate item has non-canonical {field}"
        )
    return value


def _experiment_stageable(design: dict[str, Any]) -> bool:
    if design.get("decision") != "approve":
        return False
    safety = design.get("safety_result")
    return isinstance(safety, dict) and safety.get("passed") is True


def _changeset_openable(changeset: dict[str, Any]) -> bool:
    if changeset.get("decision") != "approve":
        return False
    safety = changeset.get("safety_result")
    return isinstance(safety, dict) and safety.get("passed") is True


def _validate_gate_item_contract(
    gate_agent: str,
    item: dict[str, Any],
) -> None:
    """Reject legacy or partial action metadata before any command is queued."""
    if gate_agent == "feature_proposal":
        # This agent is disabled and its only supported legacy outcome is a
        # quarantined/rejected internal record, never an external mutation.
        return

    decision = item.get("decision")
    if decision not in {"approve", "halt"}:
        raise ApprovalGateConflictError(
            f"Persisted {gate_agent} gate item has no canonical decision"
        )
    safety = item.get("safety_result")
    if not isinstance(safety, dict):
        raise ApprovalGateConflictError(
            f"Persisted {gate_agent} gate item has no safety result"
        )
    if not isinstance(safety.get("passed"), bool):
        raise ApprovalGateConflictError(
            f"Persisted {gate_agent} safety result requires boolean passed"
        )
    if not isinstance(safety.get("checks"), list):
        raise ApprovalGateConflictError(
            f"Persisted {gate_agent} safety result requires checks"
        )
    if safety.get("risk_level") not in {"low", "medium", "high"}:
        raise ApprovalGateConflictError(
            f"Persisted {gate_agent} safety result has invalid risk_level"
        )
    if gate_agent == "experiment_design":
        if not isinstance(safety.get("evidence_complete"), bool):
            raise ApprovalGateConflictError(
                "Persisted experiment safety result requires evidence_complete"
            )
        if safety.get("requires_approval") is not True:
            raise ApprovalGateConflictError(
                "Persisted experiment safety result requires human approval"
            )


def _approval_request_sha256(
    run_id: str,
    decisions: tuple[ApprovalDecision, ...],
    comment: str | None,
) -> str:
    canonical = json.dumps(
        {
            "run_id": run_id,
            "decisions": [
                {"item_id": decision.item_id, "approved": decision.approved}
                for decision in sorted(decisions, key=lambda item: item.item_id)
            ],
            "comment": comment,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _plan_effects(
    gate_agent: str,
    items_by_id: dict[str, dict[str, Any]],
    decisions: tuple[ApprovalDecision, ...],
) -> list[_PlannedEffect]:
    effects: list[_PlannedEffect] = []
    for index, decision in enumerate(decisions):
        item = items_by_id[decision.item_id]
        order = index * 10
        if gate_agent == "experiment_design":
            if decision.approved and _experiment_stageable(item):
                draft_id = uuid.uuid4()
                effects.append(
                    _PlannedEffect(
                        effect_id=draft_id,
                        item_id=decision.item_id,
                        effect_type="stage_experiment_draft",
                        effect_order=order,
                        depends_on_effect_id=None,
                        payload=item,
                        quota_action_type="create_experiment",
                    )
                )
                if treatment_changeset_task(item) is not None:
                    effects.append(
                        _PlannedEffect(
                            effect_id=uuid.uuid4(),
                            item_id=decision.item_id,
                            effect_type="open_treatment_changeset",
                            effect_order=order + 1,
                            depends_on_effect_id=draft_id,
                            payload=item,
                            quota_action_type="open_pull_request",
                        )
                    )
            elif decision.approved:
                effects.append(
                    _PlannedEffect(
                        effect_id=uuid.uuid4(),
                        item_id=decision.item_id,
                        effect_type="record_experiment_rejection",
                        effect_order=order,
                        depends_on_effect_id=None,
                        payload={
                            **item,
                            "rejection_reason": "approved item was not actionable",
                        },
                        quota_action_type=None,
                    )
                )
            else:
                effects.append(
                    _PlannedEffect(
                        effect_id=uuid.uuid4(),
                        item_id=decision.item_id,
                        effect_type="record_experiment_rejection",
                        effect_order=order,
                        depends_on_effect_id=None,
                        payload=item,
                        quota_action_type=None,
                    )
                )
        elif gate_agent == "code_implementation":
            if decision.approved and _changeset_openable(item):
                effects.append(
                    _PlannedEffect(
                        effect_id=uuid.uuid4(),
                        item_id=decision.item_id,
                        effect_type="open_code_changeset",
                        effect_order=order,
                        depends_on_effect_id=None,
                        payload=item,
                        quota_action_type="open_pull_request",
                    )
                )
            else:
                reason = (
                    "PR rejected at the approval gate."
                    if not decision.approved
                    else "Approved changeset was not actionable."
                )
                effects.append(
                    _PlannedEffect(
                        effect_id=uuid.uuid4(),
                        item_id=decision.item_id,
                        effect_type="record_proposal_rejection",
                        effect_order=order,
                        depends_on_effect_id=None,
                        payload={**item, "rejection_reason": reason},
                        quota_action_type=None,
                    )
                )
        else:
            reason = (
                "Legacy feature proposal approval is quarantined; deployment readiness "
                "was not assessed."
                if decision.approved
                else "Feature proposal rejected at the approval gate."
            )
            effects.append(
                _PlannedEffect(
                    effect_id=uuid.uuid4(),
                    item_id=decision.item_id,
                    effect_type=(
                        "quarantine_feature_proposal"
                        if decision.approved
                        else "record_proposal_rejection"
                    ),
                    effect_order=order,
                    depends_on_effect_id=None,
                    payload={**item, "rejection_reason": reason},
                    quota_action_type=None,
                )
            )
    return effects


async def _insert_required_audit(
    conn: Any,
    *,
    run_id: str,
    action_type: str,
    config: dict[str, Any],
    approval_status: str | None,
    idempotency_key: str,
    correlation_id: uuid.UUID,
) -> None:
    await conn.execute(
        """
        INSERT INTO agent_audit_log (
            run_id, action_type, config, safety_result, approval_status,
            idempotency_key, correlation_id
        )
        VALUES ($1, $2, $3::jsonb, '{}'::jsonb, $4, $5, $6)
        ON CONFLICT (run_id, idempotency_key) WHERE idempotency_key IS NOT NULL
        DO NOTHING
        """,
        run_id,
        action_type,
        json.dumps(config, default=str),
        approval_status,
        idempotency_key,
        correlation_id,
    )


def _effect_view(row: Any) -> ApprovalEffectView:
    result = row["result"]
    if isinstance(result, str):
        result = json.loads(result)
    return ApprovalEffectView(
        effect_id=str(row["effect_id"]),
        item_id=str(row["item_id"]),
        effect_type=str(row["effect_type"]),
        status=str(row["status"]),
        attempt_count=int(row["attempt_count"]),
        last_error=(str(row["last_error"]) if row["last_error"] is not None else None),
        result=(dict(result) if isinstance(result, dict) else None),
    )


def _command_view(command: Any, effects: list[Any]) -> ApprovalCommandView:
    return ApprovalCommandView(
        command_id=str(command["command_id"]),
        run_id=str(command["run_id"]),
        actor_credential_id=str(command["actor_credential_id"]),
        actor_user_id=(
            str(command["actor_user_id"])
            if command["actor_user_id"] is not None
            else None
        ),
        gate_id=str(command["gate_id"]),
        gate_agent=str(command["gate_agent"]),
        status=str(command["status"]),
        approved_count=int(command["approved_count"]),
        rejected_count=int(command["rejected_count"]),
        comment=(str(command["comment"]) if command["comment"] is not None else None),
        last_error=(
            str(command["last_error"]) if command["last_error"] is not None else None
        ),
        created_at=command["created_at"],
        updated_at=command["updated_at"],
        effects=tuple(_effect_view(row) for row in effects),
    )


async def enqueue_approval_command(
    pool: asyncpg.Pool,
    *,
    run_id: str,
    project_id: str,
    actor_credential_id: str,
    codegen_changeset_capability: CodegenChangesetCapability,
    decisions: tuple[ApprovalDecision, ...],
    comment: str | None,
    actor_user_id: str | None = None,
) -> ApprovalCommandView:
    """Validate and atomically enqueue the exact currently persisted gate."""
    if codegen_changeset_capability not in {"available", "disabled", "unavailable"}:
        raise ValueError("Invalid Codegen changeset capability")
    request_sha256 = _approval_request_sha256(run_id, decisions, comment)
    async with pool.acquire() as conn:
        async with conn.transaction():
            run = await conn.fetchrow(
                """
                SELECT run_id, project_id, status, phase
                FROM agent_runs
                WHERE run_id = $1 AND project_id = $2
                FOR UPDATE
                """,
                run_id,
                project_id,
            )
            if run is None:
                raise ApprovalRunNotFoundError(f"Run {run_id} not found")

            existing_command = await conn.fetchrow(
                """
                SELECT *
                FROM agent_approval_commands
                WHERE run_id = $1 AND project_id = $2 AND request_sha256 = $3
                FOR UPDATE
                """,
                run_id,
                project_id,
                request_sha256,
            )
            if existing_command is not None:
                existing_effects = await conn.fetch(
                    """
                    SELECT effect_id, item_id, effect_type, status, attempt_count,
                           last_error, result
                    FROM agent_approval_effects
                    WHERE command_id = $1
                    ORDER BY effect_order, created_at, effect_id
                    """,
                    existing_command["command_id"],
                )
                return _command_view(existing_command, list(existing_effects))

            if run["status"] != "waiting_approval":
                raise ApprovalGateConflictError(
                    f"Run {run_id} is not waiting for approval"
                )

            phase = str(run["phase"] or "")
            if not phase.endswith("_approval"):
                raise ApprovalGateConflictError(
                    f"Run {run_id} has no canonical gate phase"
                )
            gate_agent = phase.removesuffix("_approval")
            produces = _GATE_RESULT_KEY.get(gate_agent)
            if produces is None:
                raise ApprovalGateConflictError(
                    f"Unsupported approval gate {gate_agent!r}"
                )

            result = await conn.fetchrow(
                """
                SELECT agent_name, produces, output, metadata
                FROM agent_run_results
                WHERE run_id = $1 AND agent_name = $2
                FOR UPDATE
                """,
                run_id,
                gate_agent,
            )
            if result is None or result["produces"] != produces:
                raise ApprovalGateConflictError("The persisted gate result is missing")

            metadata = _json_object(result["metadata"], label="gate metadata")
            gate = metadata.get("approval_gate")
            expected_gate_id = f"{run_id}:{gate_agent}"
            expected_gate = {
                "gate_id": expected_gate_id,
                "agent_name": gate_agent,
                "produces": produces,
                "phase": phase,
                "state": "pending",
            }
            if gate != expected_gate:
                raise ApprovalGateConflictError("The persisted gate reference is stale")

            raw_items = _json_array(result["output"], label="gate output")
            if not raw_items:
                raise ApprovalGateConflictError("The persisted gate contains no items")
            items_by_id: dict[str, dict[str, Any]] = {}
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    raise ApprovalGateConflictError(
                        "Persisted gate items must be objects"
                    )
                item = dict(raw_item)
                item_id = _canonical_item_id(gate_agent, item)
                _validate_gate_item_contract(gate_agent, item)
                if item_id in items_by_id:
                    raise ApprovalGateConflictError(
                        f"Persisted gate contains duplicate item id {item_id!r}"
                    )
                items_by_id[item_id] = item

            decision_ids = {decision.item_id for decision in decisions}
            persisted_ids = set(items_by_id)
            if decision_ids != persisted_ids:
                unknown = sorted(decision_ids - persisted_ids)
                missing = sorted(persisted_ids - decision_ids)
                details: list[str] = []
                if unknown:
                    details.append(f"unknown={unknown}")
                if missing:
                    details.append(f"missing={missing}")
                raise ApprovalDecisionError(
                    "Decisions must exactly match persisted gate items ("
                    + ", ".join(details)
                    + ")"
                )

            command_id = uuid.uuid4()
            approved_count = sum(decision.approved for decision in decisions)
            rejected_count = len(decisions) - approved_count
            resume_status = "approved" if approved_count else "rejected"
            effects = _plan_effects(gate_agent, items_by_id, decisions)
            if codegen_changeset_capability != "available" and any(
                effect.effect_type in _CODEGEN_EFFECT_TYPES for effect in effects
            ):
                raise ApprovalCapabilityError(codegen_changeset_capability)

            command = await conn.fetchrow(
                """
                INSERT INTO agent_approval_commands (
                    command_id, run_id, project_id, actor_credential_id, actor_user_id,
                    request_sha256, gate_id, gate_agent, status, resume_status,
                    approved_count, rejected_count, comment
                )
                VALUES (
                    $1, $2, $3, $4, $12, $5, $6, $7,
                    'queued', $8, $9, $10, $11
                )
                RETURNING *
                """,
                command_id,
                run_id,
                project_id,
                actor_credential_id,
                request_sha256,
                expected_gate_id,
                gate_agent,
                resume_status,
                approved_count,
                rejected_count,
                comment,
                actor_user_id,
            )

            for decision in decisions:
                await conn.execute(
                    """
                    INSERT INTO agent_approval_decisions (command_id, item_id, approved)
                    VALUES ($1, $2, $3)
                    """,
                    command_id,
                    decision.item_id,
                    decision.approved,
                )

            effect_rows: list[Any] = []
            for effect in effects:
                idempotency_key = f"{command_id}:{effect.effect_id}"
                effect_rows.append(
                    await conn.fetchrow(
                        """
                        INSERT INTO agent_approval_effects (
                            effect_id, command_id, run_id, project_id, item_id,
                            effect_type, effect_order, depends_on_effect_id, payload,
                            idempotency_key, quota_action_type
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)
                        RETURNING effect_id, item_id, effect_type, status,
                                  attempt_count, last_error, result
                        """,
                        effect.effect_id,
                        command_id,
                        run_id,
                        project_id,
                        effect.item_id,
                        effect.effect_type,
                        effect.effect_order,
                        effect.depends_on_effect_id,
                        json.dumps(effect.payload, default=str),
                        idempotency_key,
                        effect.quota_action_type,
                    )
                )

            for index, decision in enumerate(decisions):
                await _insert_required_audit(
                    conn,
                    run_id=run_id,
                    action_type="human_approval",
                    config={
                        "command_id": str(command_id),
                        "gate_id": expected_gate_id,
                        "gate_agent": gate_agent,
                        "item_id": decision.item_id,
                        "approved": decision.approved,
                        "comment": comment,
                        "actor_credential_id": actor_credential_id,
                        "actor_user_id": actor_user_id,
                    },
                    approval_status="approved" if decision.approved else "rejected",
                    idempotency_key=f"approval:{command_id}:{index}",
                    correlation_id=command_id,
                )
            for effect in effects:
                await _insert_required_audit(
                    conn,
                    run_id=run_id,
                    action_type="approval_effect_planned",
                    config={
                        "command_id": str(command_id),
                        "effect_id": str(effect.effect_id),
                        "item_id": effect.item_id,
                        "effect_type": effect.effect_type,
                    },
                    approval_status="queued",
                    idempotency_key=f"approval-effect-planned:{effect.effect_id}",
                    correlation_id=command_id,
                )
            await _insert_required_audit(
                conn,
                run_id=run_id,
                action_type="approval_command_queued",
                config={
                    "command_id": str(command_id),
                    "gate_id": expected_gate_id,
                    "approved_count": approved_count,
                    "rejected_count": rejected_count,
                    "effect_count": len(effects),
                    "actor_credential_id": actor_credential_id,
                    "actor_user_id": actor_user_id,
                },
                approval_status="queued",
                idempotency_key=f"approval-command:{command_id}",
                correlation_id=command_id,
            )

            metadata["approval_gate"] = {
                **expected_gate,
                "state": "queued",
                "command_id": str(command_id),
            }
            await conn.execute(
                """
                UPDATE agent_run_results
                SET metadata = $3::jsonb, created_at = now()
                WHERE run_id = $1 AND agent_name = $2
                """,
                run_id,
                gate_agent,
                json.dumps(metadata, default=str),
            )
            await conn.execute(
                """
                UPDATE agent_runs
                SET status = 'approval_queued', updated_at = now(),
                    lease_owner_id = NULL, lease_expires_at = NULL
                WHERE run_id = $1
                """,
                run_id,
            )

    assert command is not None
    return _command_view(command, effect_rows)


async def get_approval_command(
    pool: asyncpg.Pool,
    *,
    run_id: str,
    project_id: str,
    command_id: str,
) -> ApprovalCommandView | None:
    try:
        parsed_command_id = uuid.UUID(command_id)
    except ValueError:
        return None
    async with pool.acquire() as conn:
        command = await conn.fetchrow(
            """
            SELECT * FROM agent_approval_commands
            WHERE command_id = $1 AND run_id = $2 AND project_id = $3
            """,
            parsed_command_id,
            run_id,
            project_id,
        )
        if command is None:
            return None
        effects = await conn.fetch(
            """
            SELECT effect_id, item_id, effect_type, status, attempt_count,
                   last_error, result
            FROM agent_approval_effects
            WHERE command_id = $1
            ORDER BY effect_order, created_at, effect_id
            """,
            parsed_command_id,
        )
    return _command_view(command, list(effects))


def _worker_owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


async def _claim_effect(
    pool: asyncpg.Pool,
    owner_id: str,
    *,
    lease_seconds: int = EFFECT_LEASE_SECONDS,
) -> _ClaimedEffect | None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                WITH candidate AS (
                    SELECT effect.effect_id
                    FROM agent_approval_effects AS effect
                    JOIN agent_approval_commands AS command
                      ON command.command_id = effect.command_id
                    JOIN agent_runs AS run
                      ON run.run_id = effect.run_id
                     AND run.project_id = effect.project_id
                    LEFT JOIN agent_approval_effects AS dependency
                      ON dependency.effect_id = effect.depends_on_effect_id
                    WHERE (
                        (effect.status IN ('queued', 'retryable_failed')
                         AND effect.next_attempt_at <= now())
                        OR (
                            effect.status = 'processing'
                            AND effect.lease_expires_at <= now()
                        )
                    )
                      AND (dependency.effect_id IS NULL OR dependency.status = 'succeeded')
                      AND command.status IN ('queued', 'processing')
                      AND run.status = 'approval_queued'
                    ORDER BY effect.next_attempt_at, effect.effect_order,
                             effect.created_at, effect.effect_id
                    FOR UPDATE OF effect SKIP LOCKED
                    LIMIT 1
                )
                UPDATE agent_approval_effects AS effect
                SET status = 'processing',
                    lease_owner_id = $1,
                    lease_expires_at = now() + ($2 * interval '1 second'),
                    attempt_count = effect.attempt_count + 1,
                    updated_at = now()
                FROM candidate
                WHERE effect.effect_id = candidate.effect_id
                RETURNING effect.effect_id, effect.command_id, effect.run_id,
                          effect.project_id, effect.item_id, effect.effect_type,
                          effect.payload, effect.idempotency_key,
                          effect.quota_action_type, effect.attempt_count,
                          effect.max_attempts
                """,
                owner_id,
                lease_seconds,
            )
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE agent_approval_commands
                SET status = 'processing', updated_at = now()
                WHERE command_id = $1 AND status = 'queued'
                """,
                row["command_id"],
            )
    return _ClaimedEffect(
        effect_id=str(row["effect_id"]),
        command_id=str(row["command_id"]),
        run_id=str(row["run_id"]),
        project_id=str(row["project_id"]),
        item_id=str(row["item_id"]),
        effect_type=str(row["effect_type"]),
        payload=row["payload"],
        idempotency_key=str(row["idempotency_key"]),
        quota_action_type=(
            str(row["quota_action_type"])
            if row["quota_action_type"] is not None
            else None
        ),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        lease_owner_id=owner_id,
    )


async def _execute_effect(pool: asyncpg.Pool, effect: _ClaimedEffect) -> dict[str, Any]:
    payload = effect.payload
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            raise PermanentApprovalEffectError(
                f"Approval effect {effect.effect_id} payload is malformed JSON"
            ) from exc
    if not isinstance(payload, dict):
        raise PermanentApprovalEffectError(
            f"Approval effect {effect.effect_id} payload must be an object"
        )
    payload = dict(payload)

    if effect.quota_action_type is not None:
        try:
            await reserve_mutation(
                pool,
                project_id=effect.project_id,
                action_type=effect.quota_action_type,  # type: ignore[arg-type]
                idempotency_key=effect.idempotency_key,
            )
        except MutationQuotaExceededError as exc:
            raise RetryableApprovalEffectError(str(exc), delay_seconds=15 * 60) from exc

    if effect.effect_type == "stage_experiment_draft":
        await stage_experiment_draft(
            effect.project_id,
            payload,
            idempotency_key=effect.idempotency_key,
        )
        await record_designed_experiment(
            pool,
            effect.project_id,
            effect.run_id,
            payload,
            "drafted",
        )
        return {"experiment_id": effect.item_id, "status": "drafted"}

    if effect.effect_type == "open_treatment_changeset":
        changeset_id = await open_treatment_changeset(
            pool,
            effect.project_id,
            effect.run_id,
            payload,
            idempotency_key=effect.idempotency_key,
        )
        if not changeset_id:
            raise PermanentApprovalEffectError(
                f"Treatment effect {effect.effect_id} has no changeset task"
            )
        return {"changeset_id": changeset_id}

    if effect.effect_type == "open_code_changeset":
        proposal_id = effect.item_id
        title = str(payload.get("title") or "").strip()
        spec = str(payload.get("spec") or "").strip()
        if not title or not spec:
            proposal = await get_proposal(pool, effect.project_id, proposal_id)
            if proposal is not None:
                title = title or str(proposal.get("title") or "").strip()
                spec = spec or str(proposal.get("spec") or "").strip()
        if not title or not spec:
            raise PermanentApprovalEffectError(
                f"Approved proposal {proposal_id} has no canonical title/spec"
            )
        changeset = await open_changeset(
            project_id=effect.project_id,
            title=title,
            spec=spec,
            idempotency_key=effect.idempotency_key,
            run_id=effect.run_id,
            context={"proposal_id": proposal_id},
            constraints=["All existing tests must pass."],
        )
        changeset_id = str(changeset.get("changeset_id") or "").strip()
        if not changeset_id:
            raise RetryableApprovalEffectError("Codegen returned no changeset_id")
        await mark_implemented(
            pool,
            effect.project_id,
            proposal_id,
            changeset_id,
            effect.run_id,
        )
        return {"changeset_id": changeset_id}

    if effect.effect_type == "record_experiment_rejection":
        await record_designed_experiment(
            pool,
            effect.project_id,
            effect.run_id,
            payload,
            "rejected",
        )
        return {"experiment_id": effect.item_id, "status": "rejected"}

    if effect.effect_type in {
        "record_proposal_rejection",
        "quarantine_feature_proposal",
    }:
        if effect.effect_type == "quarantine_feature_proposal":
            await enqueue_proposals(
                pool,
                effect.run_id,
                effect.project_id,
                [payload],
            )
        reason = str(payload.get("rejection_reason") or "Proposal rejected.")
        await mark_failed(
            pool,
            effect.project_id,
            effect.item_id,
            reason,
            effect.run_id,
        )
        return {"proposal_id": effect.item_id, "status": "failed", "reason": reason}

    raise PermanentApprovalEffectError(
        f"Unknown approval effect type {effect.effect_type!r}"
    )


def _classify_effect_error(exc: Exception) -> Exception:
    """Separate retryable outages from deterministic request defects."""
    if isinstance(exc, PermanentApprovalEffectError | RetryableApprovalEffectError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if 400 <= status_code < 500 and status_code not in {408, 425, 429}:
            return PermanentApprovalEffectError(
                f"Downstream rejected the persisted effect with HTTP {status_code}"
            )
        return RetryableApprovalEffectError(
            f"Downstream is temporarily unavailable with HTTP {status_code}"
        )
    if isinstance(exc, (ValueError, TypeError)):
        return PermanentApprovalEffectError(str(exc))
    if isinstance(exc, httpx.RequestError):
        return RetryableApprovalEffectError(
            f"Downstream request failed: {type(exc).__name__}"
        )
    return exc


async def _finalize_command_if_terminal(conn: Any, command_id: str) -> None:
    rows = await conn.fetch(
        """
        SELECT status FROM agent_approval_effects
        WHERE command_id = $1
        FOR UPDATE
        """,
        uuid.UUID(command_id),
    )
    statuses = {str(row["status"]) for row in rows}
    if statuses and statuses <= {"succeeded"}:
        command = await conn.fetchrow(
            """
            UPDATE agent_approval_commands
            SET status = 'succeeded', last_error = NULL,
                completed_at = now(), updated_at = now()
            WHERE command_id = $1 AND status IN ('queued', 'processing')
            RETURNING run_id, gate_agent, resume_status
            """,
            uuid.UUID(command_id),
        )
        if command is None:
            return
        await conn.execute(
            """
            UPDATE agent_runs
            SET status = $2, phase = 'resuming', updated_at = now(),
                lease_owner_id = NULL, lease_expires_at = NULL
            WHERE run_id = $1 AND status = 'approval_queued'
            """,
            command["run_id"],
            command["resume_status"],
        )
        await conn.execute(
            """
            UPDATE agent_run_results
            SET metadata = jsonb_set(
                    metadata,
                    '{approval_gate,state}',
                    '"resolved"'::jsonb,
                    false
                ),
                created_at = now()
            WHERE run_id = $1 AND agent_name = $2
            """,
            command["run_id"],
            command["gate_agent"],
        )
        return

    if statuses & {"manual_intervention", "failed"}:
        last_error = await conn.fetchval(
            """
            SELECT last_error FROM agent_approval_effects
            WHERE command_id = $1
              AND status IN ('manual_intervention', 'failed')
            ORDER BY updated_at DESC LIMIT 1
            """,
            uuid.UUID(command_id),
        )
        await conn.execute(
            """
            UPDATE agent_approval_effects
            SET status = 'manual_intervention',
                last_error = COALESCE(last_error, 'Dependency did not complete'),
                lease_owner_id = NULL, lease_expires_at = NULL,
                completed_at = now(), updated_at = now()
            WHERE command_id = $1
              AND status IN ('queued', 'retryable_failed')
            """,
            uuid.UUID(command_id),
        )
        command = await conn.fetchrow(
            """
            UPDATE agent_approval_commands
            SET status = 'manual_intervention', last_error = $2,
                completed_at = now(), updated_at = now()
            WHERE command_id = $1 AND status IN ('queued', 'processing')
            RETURNING run_id, gate_agent
            """,
            uuid.UUID(command_id),
            str(last_error or "Approval effect requires manual intervention"),
        )
        if command is not None:
            await conn.execute(
                """
                UPDATE agent_runs
                SET status = 'manual_intervention',
                    phase = $2 || '_approval', updated_at = now(),
                    lease_owner_id = NULL, lease_expires_at = NULL
                WHERE run_id = $1
                """,
                command["run_id"],
                command["gate_agent"],
            )


async def _complete_effect(
    pool: asyncpg.Pool,
    effect: _ClaimedEffect,
    result: dict[str, Any],
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE agent_approval_effects
                SET status = 'succeeded', result = $3::jsonb, last_error = NULL,
                    lease_owner_id = NULL, lease_expires_at = NULL,
                    completed_at = now(), updated_at = now()
                WHERE effect_id = $1 AND lease_owner_id = $2 AND status = 'processing'
                RETURNING effect_id
                """,
                uuid.UUID(effect.effect_id),
                effect.lease_owner_id,
                json.dumps(result, default=str),
            )
            if updated is None:
                raise RuntimeError(f"Approval effect {effect.effect_id} lease was lost")
            await _insert_required_audit(
                conn,
                run_id=effect.run_id,
                action_type="approval_effect_succeeded",
                config={
                    "command_id": effect.command_id,
                    "effect_id": effect.effect_id,
                    "item_id": effect.item_id,
                    "effect_type": effect.effect_type,
                    "attempt_count": effect.attempt_count,
                    "result": result,
                },
                approval_status="succeeded",
                idempotency_key=f"approval-effect-succeeded:{effect.effect_id}",
                correlation_id=uuid.UUID(effect.command_id),
            )
            await _finalize_command_if_terminal(conn, effect.command_id)


async def _fail_effect(
    pool: asyncpg.Pool,
    effect: _ClaimedEffect,
    exc: Exception,
) -> None:
    permanent = isinstance(exc, PermanentApprovalEffectError)
    exhausted = effect.attempt_count >= effect.max_attempts
    manual = permanent or exhausted
    status = "manual_intervention" if manual else "retryable_failed"
    if isinstance(exc, RetryableApprovalEffectError) and exc.delay_seconds is not None:
        delay_seconds = exc.delay_seconds
    else:
        delay_seconds = min(5 * 60, 2 ** min(effect.attempt_count, 8))
    error = f"{type(exc).__name__}: {exc}"[:4000]

    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchval(
                """
                UPDATE agent_approval_effects
                SET status = $3,
                    last_error = $4,
                    next_attempt_at = now() + ($5 * interval '1 second'),
                    lease_owner_id = NULL,
                    lease_expires_at = NULL,
                    completed_at = CASE WHEN $3 = 'manual_intervention' THEN now() ELSE NULL END,
                    updated_at = now()
                WHERE effect_id = $1 AND lease_owner_id = $2 AND status = 'processing'
                RETURNING effect_id
                """,
                uuid.UUID(effect.effect_id),
                effect.lease_owner_id,
                status,
                error,
                delay_seconds,
            )
            if updated is None:
                raise RuntimeError(f"Approval effect {effect.effect_id} lease was lost")
            await _insert_required_audit(
                conn,
                run_id=effect.run_id,
                action_type=(
                    "approval_effect_manual_intervention"
                    if manual
                    else "approval_effect_retry_scheduled"
                ),
                config={
                    "command_id": effect.command_id,
                    "effect_id": effect.effect_id,
                    "item_id": effect.item_id,
                    "effect_type": effect.effect_type,
                    "attempt_count": effect.attempt_count,
                    "error": error,
                },
                approval_status=status,
                idempotency_key=(
                    f"approval-effect-terminal:{effect.effect_id}"
                    if manual
                    else f"approval-effect-retry:{effect.effect_id}:{effect.attempt_count}"
                ),
                correlation_id=uuid.UUID(effect.command_id),
            )
            await _finalize_command_if_terminal(conn, effect.command_id)


async def process_one_approval_effect(
    pool: asyncpg.Pool,
    *,
    owner_id: str | None = None,
) -> bool:
    """Lease and process one effect.  Returns False when no work is ready."""
    claimed = await _claim_effect(pool, owner_id or _worker_owner_id())
    if claimed is None:
        return False
    try:
        result = await _execute_effect(pool, claimed)
    except Exception as exc:
        classified = _classify_effect_error(exc)
        logger.exception(
            "[%s] Approval effect %s failed on attempt %d",
            claimed.run_id,
            claimed.effect_id,
            claimed.attempt_count,
        )
        await _fail_effect(pool, claimed, classified)
    else:
        await _complete_effect(pool, claimed, result)
    return True


async def run_approval_effect_worker_forever(
    pool: asyncpg.Pool,
    stop: asyncio.Event,
    *,
    interval_seconds: float = EFFECT_POLL_SECONDS,
) -> None:
    """Process the durable outbox until application shutdown."""
    owner_id = _worker_owner_id()
    while not stop.is_set():
        try:
            processed = await process_one_approval_effect(pool, owner_id=owner_id)
        except Exception:
            processed = False
            logger.exception("Approval effect worker poll failed")
        if processed:
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
