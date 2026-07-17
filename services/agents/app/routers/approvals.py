"""Strict human approval commands for persisted agent gates.

This router never mutates Config, Codegen, or an in-process supervisor.  A POST
first reads Codegen's strict readiness/capability contract, then validates the
exact persisted gate and transactionally queues its command, decisions,
mandatory audit intents, and effects.  The durable effect worker applies and
retries those effects after the response has returned.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

import asyncpg
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.auth import require_role
from app.readiness import codegen_changeset_capability
from app.store.approval_effects import (
    ApprovalCapabilityError,
    ApprovalCommandView,
    ApprovalDecision,
    ApprovalDecisionError,
    ApprovalGateConflictError,
    ApprovalRunNotFoundError,
    enqueue_approval_command,
    get_approval_command,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/agents", tags=["agents"])

CommandStatus = Literal["queued", "processing", "succeeded", "manual_intervention"]
EffectStatus = Literal[
    "queued",
    "processing",
    "retryable_failed",
    "succeeded",
    "failed",
    "manual_intervention",
]
GateAgent = Literal["experiment_design", "feature_proposal", "code_implementation"]


class ItemDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    item_id: str = Field(min_length=1, max_length=128)
    approved: bool

    @field_validator("item_id")
    @classmethod
    def _canonical_item_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("item_id must not contain surrounding whitespace")
        if not value:
            raise ValueError("item_id must not be blank")
        return value


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decisions: list[ItemDecision] = Field(min_length=1, max_length=100)
    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _unique_item_ids(self) -> "ApprovalRequest":
        item_ids = [decision.item_id for decision in self.decisions]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("decisions must contain unique item_id values")
        return self


class ApprovalEffectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect_id: str
    item_id: str
    effect_type: str
    status: EffectStatus
    attempt_count: int
    last_error: str | None
    result: dict | None


class ApprovalCommandResponse(BaseModel):
    """One canonical queued-command/status envelope for POST and GET."""

    model_config = ConfigDict(extra="forbid")

    command_id: str
    run_id: str
    actor_credential_id: str
    actor_user_id: str | None
    gate_id: str
    gate_agent: GateAgent
    status: CommandStatus
    approved_count: int
    rejected_count: int
    comment: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    effects: list[ApprovalEffectResponse]


def _response(view: ApprovalCommandView) -> ApprovalCommandResponse:
    return ApprovalCommandResponse(
        command_id=view.command_id,
        run_id=view.run_id,
        actor_credential_id=view.actor_credential_id,
        actor_user_id=view.actor_user_id,
        gate_id=view.gate_id,
        gate_agent=view.gate_agent,  # type: ignore[arg-type]
        status=view.status,  # type: ignore[arg-type]
        approved_count=view.approved_count,
        rejected_count=view.rejected_count,
        comment=view.comment,
        last_error=view.last_error,
        created_at=view.created_at,
        updated_at=view.updated_at,
        effects=[
            ApprovalEffectResponse(
                effect_id=effect.effect_id,
                item_id=effect.item_id,
                effect_type=effect.effect_type,
                status=effect.status,  # type: ignore[arg-type]
                attempt_count=effect.attempt_count,
                last_error=effect.last_error,
                result=effect.result,
            )
            for effect in view.effects
        ],
    )


@router.post(
    "/{run_id}/approve",
    response_model=ApprovalCommandResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def approve_action(
    run_id: str,
    body: ApprovalRequest,
    request: Request,
) -> ApprovalCommandResponse:
    """Queue the exact decisions for the run's currently persisted gate."""
    principal = require_role(request, "agents:approve")
    pool: asyncpg.Pool = request.app.state.pg_pool
    decisions = tuple(
        ApprovalDecision(item_id=item.item_id, approved=item.approved)
        for item in body.decisions
    )
    try:
        codegen_capability = await codegen_changeset_capability()
        command = await enqueue_approval_command(
            pool,
            run_id=run_id,
            project_id=principal.project_id,
            actor_credential_id=principal.credential_id,
            actor_user_id=principal.actor_user_id,
            codegen_changeset_capability=codegen_capability,
            decisions=decisions,
            comment=body.comment,
        )
    except ApprovalRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalDecisionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ApprovalGateConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ApprovalCapabilityError as exc:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail=str(exc),
        ) from exc
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Run {run_id} approval gate was already submitted",
        ) from exc
    except Exception as exc:
        # A transaction failure includes failure to persist mandatory audit
        # intent.  The transaction rolls back and no effect is allowed to run.
        logger.exception("[%s] Could not enqueue approval command", run_id)
        raise HTTPException(
            status_code=503,
            detail="Approval was not recorded; retry the complete request.",
        ) from exc
    return _response(command)


@router.get(
    "/{run_id}/approvals/{command_id}",
    response_model=ApprovalCommandResponse,
)
async def approval_command_status(
    run_id: str,
    command_id: str,
    request: Request,
) -> ApprovalCommandResponse:
    """Return command and per-effect retry/manual-intervention state."""
    principal = require_role(request, "agents:approve")
    pool: asyncpg.Pool = request.app.state.pg_pool
    command = await get_approval_command(
        pool,
        run_id=run_id,
        project_id=principal.project_id,
        command_id=command_id,
    )
    if command is None:
        raise HTTPException(status_code=404, detail="Approval command not found")
    return _response(command)
