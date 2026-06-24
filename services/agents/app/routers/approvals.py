"""Approval endpoint — per-item human decisions on a pending agent gate.

A gate (experiment_design or feature_proposal) can hold several items. The
human decides each one in a single batched submit; approved items fork their
own downstream track (a feature proposal → its own code_implementation run /
PR; an experiment design → an individual deploy) while rejected items are
recorded and skipped. Whatever the mix, the run always resumes afterwards so it
either runs later pipeline agents or finalizes — it never wedges at ``resuming``.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, model_validator

from app.graphs.experiment_design import deploy_experiment
from app.graphs.supervisor import run_supervisor
from app.safety.audit import AuditLogger
from app.store.proposals import enqueue_proposals

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/agents", tags=["agents"])

#: Gate agent name -> the `produces` key its items are persisted under.
_GATE_RESULT_KEY = {
    "experiment_design": "experiment_designs",
    "feature_proposal": "feature_proposals",
}


class ItemDecision(BaseModel):
    item_id: str
    approved: bool


class ApprovalRequest(BaseModel):
    """Per-item decisions (``decisions``) or a legacy whole-gate ``approved``.

    Exactly one must be supplied. ``decisions`` maps each gated item — keyed by
    ``proposal_id`` / ``experiment_id`` — to approve or reject.
    """

    decisions: list[ItemDecision] | None = None
    approved: bool | None = None
    comment: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "ApprovalRequest":
        if (self.decisions is None) == (self.approved is None):
            raise ValueError("Provide exactly one of 'decisions' or 'approved'.")
        if self.decisions is not None and not self.decisions:
            raise ValueError("'decisions' must not be empty.")
        return self


class ApprovalResponse(BaseModel):
    run_id: str
    status: str
    approved_count: int
    rejected_count: int
    forked_runs: list[str]
    message: str


def _parse_config(raw: Any) -> dict[str, Any]:
    """agent_runs.config is JSONB stored as a JSON string; parse defensively."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _item_id(gated_agent: str, item: dict[str, Any]) -> str:
    """The stable id used to match a decision to a persisted gate item."""
    if gated_agent == "experiment_design":
        flag = item.get("flag_config") or {}
        return str(item.get("experiment_id") or flag.get("key") or "").strip()
    return str(item.get("proposal_id") or "").strip()


async def _load_gate_items(pool: asyncpg.Pool, run_id: str, produces: str) -> list[dict[str, Any]]:
    """Reload a gated agent's persisted output items from agent_run_results."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT output FROM agent_run_results WHERE run_id = $1 AND produces = $2",
            run_id,
            produces,
        )
    items: list[dict[str, Any]] = []
    for record in rows:
        output = record["output"]
        data = json.loads(output) if isinstance(output, str) else output
        if isinstance(data, list):
            items.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            items.append(data)
    return items


def _resolve_decisions(
    body: ApprovalRequest, gated_agent: str, items: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split persisted items into (approved, rejected).

    Legacy ``approved`` applies one verdict to all items. Per-item ``decisions``
    must name every item exactly once (matched by id); unknown or missing ids
    are a 422 — an explicit human gate should not silently drop a decision.
    """
    if body.approved is not None:
        return (list(items), []) if body.approved else ([], list(items))

    decisions = {d.item_id: d.approved for d in (body.decisions or [])}
    by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        iid = _item_id(gated_agent, item)
        if not iid:
            # A single unkeyed item (e.g. an experiment design lacking an
            # experiment_id) aligns positionally with a lone decision.
            iid = next(iter(decisions)) if len(items) == 1 and len(decisions) == 1 else f"__index_{index}"
        by_id[iid] = item

    unknown = sorted(set(decisions) - set(by_id))
    if unknown:
        raise HTTPException(status_code=422, detail=f"Decision for unknown item id(s): {unknown}")
    missing = sorted(set(by_id) - set(decisions))
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing a decision for item id(s): {missing}")

    approved = [item for iid, item in by_id.items() if decisions[iid]]
    rejected = [item for iid, item in by_id.items() if not decisions[iid]]
    return approved, rejected


def _any_approved(body: ApprovalRequest) -> bool:
    if body.approved is not None:
        return body.approved
    return any(d.approved for d in (body.decisions or []))


@router.post("/{run_id}/approve", response_model=ApprovalResponse)
async def approve_action(
    run_id: str,
    body: ApprovalRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> ApprovalResponse:
    """Record per-item human decisions on a gate, fork approved items, resume.

    Approved feature proposals each fork their own code_implementation run (one
    PR per proposal); approved experiment designs each deploy individually.
    Rejected items are audited and skipped. The run always resumes so it
    continues the pipeline or finalizes — closing the old ``resuming`` wedge.
    """
    pool: asyncpg.Pool = request.app.state.pg_pool

    async with pool.acquire() as conn:
        row: Any = await conn.fetchrow(
            "SELECT run_id, status, phase, project_id, autonomy_level, config "
            "FROM agent_runs WHERE run_id = $1",
            run_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if row["status"] != "waiting_approval":
        raise HTTPException(
            status_code=400,
            detail=f"Run {run_id} is not waiting for approval (current status: {row['status']})",
        )

    # Captured from the pre-update snapshot — the gate phase, before we overwrite it.
    gated_agent = (row["phase"] or "").removesuffix("_approval")
    project_id: str = row["project_id"]
    autonomy_level: int = row["autonomy_level"]
    config = _parse_config(row["config"])
    analysis_types = config.get("analysis_types") or []

    # Resolve decisions against persisted items BEFORE mutating (a 422 here must
    # leave the run untouched at waiting_approval so the human can resubmit).
    produces = _GATE_RESULT_KEY.get(gated_agent)
    if produces is not None:
        items = await _load_gate_items(pool, run_id, produces)
        approved_items, rejected_items = _resolve_decisions(body, gated_agent, items)
    else:
        # Gates without a per-item payload (e.g. code_implementation changesets):
        # record the decision and resume; never re-kick (would duplicate PRs).
        approved_items, rejected_items = [], []

    gate_status = "approved" if _any_approved(body) else "rejected"

    # Atomically claim the gate: only one request flips it off waiting_approval,
    # so a duplicate/concurrent submit 400s instead of double-forking.
    async with pool.acquire() as conn:
        claimed = await conn.fetchval(
            """
            UPDATE agent_runs
            SET status = $2, phase = 'resuming', updated_at = now()
            WHERE run_id = $1 AND status = 'waiting_approval'
            RETURNING run_id
            """,
            run_id,
            gate_status,
        )
    if claimed is None:
        raise HTTPException(
            status_code=400, detail=f"Run {run_id} is no longer waiting for approval"
        )

    # Everything below is best-effort: the handler must not raise, or FastAPI
    # would skip the resume BackgroundTask and re-wedge the run.
    audit = AuditLogger(pool)
    for item in approved_items:
        await audit.log(
            run_id,
            "human_approval",
            {"item_id": _item_id(gated_agent, item), "kind": gated_agent, "approved": True, "comment": body.comment},
            approval_status="approved",
        )
    for item in rejected_items:
        await audit.log(
            run_id,
            "human_approval",
            {"item_id": _item_id(gated_agent, item), "kind": gated_agent, "approved": False, "comment": body.comment},
            approval_status="rejected",
        )

    forked_runs: list[str] = []
    if gated_agent == "experiment_design":
        for design in approved_items:
            try:
                await deploy_experiment(project_id, design)
                logger.info("[%s] Deployed approved experiment %s", run_id, _item_id(gated_agent, design))
            except Exception:
                logger.exception("[%s] Failed to deploy experiment %s", run_id, _item_id(gated_agent, design))
    elif gated_agent == "feature_proposal":
        for proposal in approved_items:
            try:
                forked = await _fork_proposal(
                    request.app,
                    background_tasks,
                    run_id=run_id,
                    project_id=project_id,
                    autonomy_level=autonomy_level,
                    proposal=proposal,
                )
                if forked:
                    forked_runs.append(forked)
            except Exception:
                logger.exception("[%s] Failed to fork proposal %s", run_id, _item_id(gated_agent, proposal))

    await audit.log(
        run_id,
        "approval_decision",
        {
            "gated_agent": gated_agent,
            "approved": len(approved_items),
            "rejected": len(rejected_items),
            "forked_runs": forked_runs,
        },
        approval_status=gate_status,
    )

    # Always resume: run later pipeline agents (each gated in turn) or finalize
    # to done when none remain. This is what prevents the resuming wedge.
    background_tasks.add_task(
        run_supervisor,
        pool=pool,
        vector_store=request.app.state.vector_store,
        run_id=run_id,
        project_id=project_id,
        analysis_types=analysis_types,
        time_range_days=int(config.get("time_range_days", 7)),
        autonomy_level=autonomy_level,
        resume=True,
    )

    logger.info(
        "Run %s gate %s decided: %d approved, %d rejected, %d forked",
        run_id, gated_agent, len(approved_items), len(rejected_items), len(forked_runs),
    )
    return ApprovalResponse(
        run_id=run_id,
        status=gate_status,
        approved_count=len(approved_items),
        rejected_count=len(rejected_items),
        forked_runs=forked_runs,
        message=(
            f"{len(approved_items)} approved, {len(rejected_items)} rejected — run resumes."
        ),
    )


async def _fork_proposal(
    app: Any,
    background_tasks: BackgroundTasks,
    *,
    run_id: str,
    project_id: str,
    autonomy_level: int,
    proposal: dict[str, Any],
) -> str | None:
    """Enqueue one approved proposal and fork a code_implementation run for it.

    Decision D2 (hybrid): the durable ``feature_proposals`` queue is the source
    of truth; the forked run claims this exact proposal (one PR per approval).
    Enqueue is awaited before the run is scheduled so the row exists when the
    forked supervisor claims it.
    """
    pool: asyncpg.Pool = app.state.pg_pool
    proposal_id = str(proposal.get("proposal_id") or "").strip()
    if not proposal_id:
        logger.warning("[%s] Approved proposal has no proposal_id — cannot fork", run_id)
        return None

    await enqueue_proposals(pool, run_id, project_id, [proposal])

    new_run_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_runs
                (run_id, project_id, trigger_type, autonomy_level, status, phase, config)
            VALUES ($1, $2, 'manual', $3, 'started', 'initializing', $4::jsonb)
            """,
            new_run_id,
            project_id,
            autonomy_level,
            json.dumps(
                {
                    "analysis_types": ["code_implementation"],
                    "kicked_by": run_id,
                    "target_proposal_id": proposal_id,
                }
            ),
        )

    background_tasks.add_task(
        run_supervisor,
        pool=pool,
        vector_store=app.state.vector_store,
        run_id=new_run_id,
        project_id=project_id,
        analysis_types=["code_implementation"],
        time_range_days=7,
        autonomy_level=autonomy_level,
        target_proposal_id=proposal_id,
    )
    logger.info(
        "Forked code_implementation run %s for proposal %s (from %s)",
        new_run_id, proposal_id, run_id,
    )
    return new_run_id
