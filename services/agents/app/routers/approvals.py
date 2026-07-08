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

from app.graphs.experiment_design import deploy_experiment, open_treatment_changeset
from app.graphs.supervisor import run_supervisor
from app.safety.audit import AuditLogger
from app.store.experiments import record_designed_experiment
from app.store.proposals import enqueue_proposals, get_proposal, mark_failed, mark_implemented
from app.tools.code import open_changeset

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/agents", tags=["agents"])

#: Gate agent name -> the `produces` key its items are persisted under.
_GATE_RESULT_KEY = {
    "experiment_design": "experiment_designs",
    "feature_proposal": "feature_proposals",
    "code_implementation": "changesets",
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
    #: Changeset ids opened by this approval: draft PRs from a
    #: code_implementation gate (Phase 6) or treatment changesets for approved
    #: experiment designs (loop phase 2).
    opened_changesets: list[str] = []
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


def _changeset_openable(changeset: dict[str, Any]) -> bool:
    """True if a code_implementation gate item is safe to open a PR for.

    Only items the agent gated as ``approve`` (passed safety, genuinely awaiting a
    human) are openable. Safety-halted items (``decision == "halt"`` /
    ``safety_result.passed is False``) are already failed and must never be opened
    even if a blanket approve sweeps them up alongside a sibling that is ``approve``.
    """
    if str(changeset.get("decision") or "") != "approve":
        return False
    safety = changeset.get("safety_result")
    if isinstance(safety, dict) and safety.get("passed") is False:
        return False
    return True


async def _update_design_ledger(
    pool: asyncpg.Pool, run_id: str, project_id: str, design: dict[str, Any], status: str
) -> None:
    """Best-effort sync of a human decision into the designed_experiments ledger."""
    try:
        await record_designed_experiment(pool, project_id, run_id, design, status)
    except Exception:
        logger.exception(
            "[%s] Could not update design ledger for %s", run_id, design.get("experiment_id")
        )


def _experiment_deployable(design: dict[str, Any]) -> bool:
    """True if an experiment_design gate item is safe to deploy on approval.

    Multi-design gates can mix outcomes: a safety-halted sibling or one the
    agent already deployed (L3/L4) lands in the same persisted list as the
    items genuinely awaiting a human. A blanket approve must not deploy those.
    Legacy single-design items carry no ``decision`` key — the run only gated
    when that design was awaiting approval, so they stay deployable.
    """
    decision = design.get("decision")
    if decision is not None and str(decision) != "approve":
        return False
    safety = design.get("safety_result")
    if isinstance(safety, dict) and safety.get("passed") is False:
        return False
    return True


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
        if isinstance(output, str):
            # A malformed row must not 500 the gate forever — the human could
            # never submit a decision. Skip it and decide the rest.
            try:
                output = json.loads(output)
            except (json.JSONDecodeError, ValueError):
                logger.error("[%s] Skipping malformed %s result row", run_id, produces)
                continue
        data = output
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
        if iid in by_id:
            # Two items sharing an id (LLM emitted duplicate experiment_ids)
            # must both stay addressable — silently collapsing one would let it
            # bypass the gate undecided.
            iid = f"{iid}#{index}"
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
        # An unknown gate with no persisted payload: record the decision and
        # resume without acting on items.
        approved_items, rejected_items = [], []

    gate_status = "approved" if _any_approved(body) else "rejected"

    # Atomically claim the gate: only one request flips it off waiting_approval,
    # so a duplicate/concurrent submit 400s instead of double-forking. The claim
    # also pins the snapshotted phase — a delayed duplicate that snapshotted an
    # earlier gate must not consume a *later* gate the run has since reached
    # (its decisions were resolved against the earlier gate's items).
    async with pool.acquire() as conn:
        claimed = await conn.fetchval(
            """
            UPDATE agent_runs
            SET status = $2, phase = 'resuming', updated_at = now()
            WHERE run_id = $1 AND status = 'waiting_approval' AND phase = $3
            RETURNING run_id
            """,
            run_id,
            gate_status,
            row["phase"],
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
    opened_changesets: list[str] = []
    if gated_agent == "experiment_design":
        for design in approved_items:
            if not _experiment_deployable(design):
                await audit.log(
                    run_id,
                    "approval_skipped",
                    {
                        "item_id": _item_id(gated_agent, design),
                        "kind": gated_agent,
                        "reason": "design did not pass safety / was not awaiting approval",
                        "decision": design.get("decision"),
                    },
                    approval_status="skipped",
                )
                logger.warning(
                    "[%s] Skipped deploying design %s — decision=%r, not deployable",
                    run_id, _item_id(gated_agent, design), design.get("decision"),
                )
                continue
            try:
                deployed = await deploy_experiment(project_id, design)
            except Exception:
                deployed = False
                logger.exception("[%s] Failed to deploy experiment %s", run_id, _item_id(gated_agent, design))
            await _update_design_ledger(
                pool, run_id, project_id, design, "deployed" if deployed else "deploy_failed"
            )
            if deployed:
                logger.info("[%s] Deployed approved experiment %s", run_id, _item_id(gated_agent, design))
                # Phase 2: an approved experiment gets its treatment built. A
                # failure is audited, never fatal — the experiment exists; the
                # missing treatment is what the audit trail must show.
                try:
                    changeset_id = await open_treatment_changeset(
                        pool, project_id, run_id, design
                    )
                    if changeset_id:
                        opened_changesets.append(changeset_id)
                        await audit.log(
                            run_id,
                            "treatment_changeset_opened",
                            {"experiment_id": _item_id(gated_agent, design),
                             "changeset_id": changeset_id},
                        )
                except Exception:
                    logger.exception(
                        "[%s] Treatment changeset failed for %s",
                        run_id, _item_id(gated_agent, design),
                    )
                    await audit.log(
                        run_id,
                        "treatment_changeset_failed",
                        {"experiment_id": _item_id(gated_agent, design)},
                    )
            else:
                # deploy_experiment swallows HTTP failures into False — record
                # it, or the human sees "approved" for an experiment that
                # doesn't exist.
                await audit.log(
                    run_id,
                    "deploy_failed",
                    {"item_id": _item_id(gated_agent, design), "kind": gated_agent},
                    approval_status="approved",
                )
                logger.error("[%s] Deploy failed for approved experiment %s", run_id, _item_id(gated_agent, design))
        for design in rejected_items:
            # Rejected designs stay in the ledger so the theme is not
            # re-designed next run; a human said no, not "ask again".
            await _update_design_ledger(pool, run_id, project_id, design, "rejected")
    elif gated_agent == "code_implementation":
        # Phase 6: the agent gated BEFORE opening the PR (a draft PR is the
        # reversible action a human approves). Approval is what actually opens
        # it — open each approved changeset exactly once; mark a rejected
        # proposal failed so it never stays wedged at 'implementing'.
        for changeset in approved_items:
            # Only changesets the agent itself decided to 'approve' (passed
            # safety, awaiting a human) are openable. In a multi-proposal drain a
            # safety-halted item (decision != "approve", already mark_failed) can
            # land in the same gate and be swept up by a blanket approve — opening
            # its PR and overwriting its 'failed' status. Skip and audit those.
            if not _changeset_openable(changeset):
                await audit.log(
                    run_id,
                    "approval_skipped",
                    {
                        "item_id": _item_id(gated_agent, changeset),
                        "kind": gated_agent,
                        "reason": "changeset did not pass safety / was not awaiting approval",
                        "decision": changeset.get("decision"),
                    },
                    approval_status="skipped",
                )
                logger.warning(
                    "[%s] Skipped approving changeset %s — decision=%r, not openable",
                    run_id, _item_id(gated_agent, changeset), changeset.get("decision"),
                )
                continue
            opened = await _open_approved_changeset(
                request.app, run_id=run_id, project_id=project_id, changeset=changeset
            )
            if opened:
                opened_changesets.append(opened)
        for changeset in rejected_items:
            proposal_id = str(changeset.get("proposal_id") or "").strip()
            if proposal_id:
                try:
                    await mark_failed(pool, proposal_id, "PR rejected at the approval gate.")
                except Exception:
                    logger.exception("[%s] Could not mark rejected proposal %s", run_id, proposal_id)
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
            # Approved proposals go to long-term memory so future proposal
            # runs can recall them (the agent itself defers persistence to
            # this human gate). Best-effort.
            try:
                await request.app.state.vector_store.store(
                    project_id=project_id,
                    content=json.dumps(proposal, default=str),
                    metadata={
                        "type": "feature_proposal",
                        "proposal_id": str(proposal.get("proposal_id") or ""),
                        "priority": str(proposal.get("priority") or "P2"),
                        "status": "approved",
                    },
                )
            except Exception:
                logger.exception(
                    "[%s] Could not store approved proposal %s to memory",
                    run_id, _item_id(gated_agent, proposal),
                )

    await audit.log(
        run_id,
        "approval_decision",
        {
            "gated_agent": gated_agent,
            "approved": len(approved_items),
            "rejected": len(rejected_items),
            "forked_runs": forked_runs,
            "opened_changesets": opened_changesets,
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
        "Run %s gate %s decided: %d approved, %d rejected, %d forked, %d PR(s) opened",
        run_id, gated_agent, len(approved_items), len(rejected_items),
        len(forked_runs), len(opened_changesets),
    )
    opened_note = f" · {len(opened_changesets)} PR(s) opened" if opened_changesets else ""
    return ApprovalResponse(
        run_id=run_id,
        status=gate_status,
        approved_count=len(approved_items),
        rejected_count=len(rejected_items),
        forked_runs=forked_runs,
        opened_changesets=opened_changesets,
        message=(
            f"{len(approved_items)} approved, {len(rejected_items)} rejected — run resumes."
            + opened_note
        ),
    )


async def _open_approved_changeset(
    app: Any,
    *,
    run_id: str,
    project_id: str,
    changeset: dict[str, Any],
) -> str | None:
    """Open the draft PR for one approved code_implementation changeset.

    The gated changeset carries the proposal's ``title``/``spec`` (self-describing
    since Phase 6); older items fall back to the durable ``feature_proposals``
    row. Best-effort: a codegen failure marks the proposal failed and is logged,
    never raised — the approval handler must still resume the run.
    """
    pool: asyncpg.Pool = app.state.pg_pool
    proposal_id = str(changeset.get("proposal_id") or "").strip()
    title = str(changeset.get("title") or "").strip()
    spec = str(changeset.get("spec") or "").strip()

    if (not title or not spec) and proposal_id:
        try:
            proposal = await get_proposal(pool, proposal_id)
        except Exception:
            logger.exception("[%s] Could not load proposal %s", run_id, proposal_id)
            proposal = None
        if proposal:
            title = title or str(proposal.get("title") or "")
            spec = spec or str(proposal.get("spec") or "")

    if not title or not spec:
        logger.warning(
            "[%s] Cannot open PR for proposal %r — missing title/spec", run_id, proposal_id
        )
        if proposal_id:
            try:
                await mark_failed(pool, proposal_id, "Missing title/spec at approval.")
            except Exception:
                logger.exception("[%s] Could not mark proposal %s failed", run_id, proposal_id)
        return None

    try:
        result = await open_changeset(
            project_id=project_id,
            title=title,
            spec=spec,
            run_id=run_id,
            constraints=["All existing tests must pass."],
        )
    except Exception as exc:
        logger.exception("[%s] Failed to open changeset for %s", run_id, proposal_id)
        if proposal_id:
            try:
                await mark_failed(pool, proposal_id, str(exc))
            except Exception:
                logger.exception("[%s] Could not mark proposal %s failed", run_id, proposal_id)
        return None

    changeset_id = str(result.get("changeset_id") or "").strip()
    if changeset_id and proposal_id:
        try:
            await mark_implemented(pool, proposal_id, changeset_id)
        except Exception:
            logger.exception("[%s] Could not mark proposal %s implemented", run_id, proposal_id)
    logger.info(
        "[%s] Opened changeset %s for approved proposal %s", run_id, changeset_id, proposal_id
    )
    return changeset_id or None


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

    # Enqueue is idempotent (ON CONFLICT DO NOTHING) and skips rows failing
    # field validation, so the row may not be claimable: an id reused from an
    # earlier run can sit at implemented/failed, or the insert may have been
    # skipped entirely. Forking anyway would create a run that claims nothing
    # and reports success while the approval silently did nothing.
    row = await get_proposal(pool, proposal_id)
    if row is None or row.get("status") != "approved":
        logger.warning(
            "[%s] Proposal %s is not claimable after enqueue (status=%r) — not forking",
            run_id, proposal_id, row.get("status") if row else None,
        )
        return None

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
