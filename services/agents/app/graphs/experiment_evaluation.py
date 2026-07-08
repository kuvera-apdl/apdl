"""Experiment evaluation agent — the agent that closes the loop.

Sweeps the project's running experiments (from the config service registry,
so hand-made experiments are evaluated exactly like loop-designed ones), runs
a deterministic maturity gate, asks the reasoning model for a verdict on each
mature experiment, and executes the verdict:

* ``ship``     — stop the experiment (completed); the recorded verdict is the
                 work queue the reshaped feature_proposal agent drains.
* ``rollback`` — stop the experiment, disable the backing flag through the
                 config service's canonical rollback path, and open a revert
                 PR for the linked treatment changeset when one exists.
* ``iterate``  — stop the experiment and release its source insight in the
                 design ledger (status ``iterate_requested``) so the next
                 design run may redesign with this learning in memory.
* ``extend``   — leave it running.

A run scoped by ``ctx.target_experiment_id`` (a human's "evaluate now") also
reports an explicit ``immature`` verdict for an experiment that has not
reached maturity — the human asked, so they get the numbers, not a silent
skip. Unscoped (scheduled) runs skip immature experiments without a verdict.

Produces ``experiment_verdicts``. No human gate of its own: stopping a
concluded experiment is protective, and the consequential path (durable
feature) goes through feature_proposal's always-on approval gate.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from app.framework import AgentContext, BaseAgent, MemoryEntry, register_agent
from app.llm.prompts.evaluation import (
    EXPERIMENT_EVALUATION_PROMPT,
    EXPERIMENT_EVALUATION_SYSTEM,
)
from app.store.experiments import get_designed_experiment, set_designed_experiment_status
from app.store.verdicts import VALID_VERDICTS, record_verdict
from app.tools.code import revert_changeset
from app.tools.experiments import (
    get_active_experiments,
    get_experiment_results,
    update_experiment_status,
)
from app.tools.flags import disable_flag

logger = logging.getLogger(__name__)


def _parse_when(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@register_agent
class ExperimentEvaluationAgent(BaseAgent):
    """Evaluates running experiments and executes ship/rollback/iterate/extend."""

    name = "experiment_evaluation"
    description = "Evaluate running experiments and decide ship, rollback, iterate, or extend."
    order = 30
    system_prompt = EXPERIMENT_EVALUATION_SYSTEM
    model_tier = "reasoning"
    memory_query = "experiment verdicts outcomes learnings"
    memory_top_k = 5
    requires = ()
    produces = "experiment_verdicts"
    parse_as = "list"
    #: LLM verdicts per run — a sweep with more mature experiments than this
    #: evaluates the oldest first and picks the rest up next run.
    max_evaluations = 5
    #: Maturity defaults. The config service does not persist a design's
    #: required_sample_size, so the gate uses a floor that any real decision
    #: needs; deployments tune via env.
    min_sample_per_variant = int(os.getenv("EVAL_MIN_SAMPLE_PER_VARIANT", "200"))
    min_runtime_days = int(os.getenv("EVAL_MIN_RUNTIME_DAYS", "7"))

    # ------------------------------------------------------------------
    # gather
    # ------------------------------------------------------------------

    async def gather(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            experiments = await get_active_experiments(project_id=ctx.project_id)
        except Exception as exc:
            logger.warning("Could not list experiments: %s", exc)
            experiments = []
        # The config service registry is the source of truth — hand-made
        # experiments are evaluated exactly like loop-designed ones.
        running = [
            e for e in experiments
            if isinstance(e, dict) and e.get("status") == "running"
        ]

        if ctx.target_experiment_id:
            running = [e for e in running if e.get("key") == ctx.target_experiment_id]
            if not running:
                message = (
                    f"experiment_evaluation: no running experiment "
                    f"'{ctx.target_experiment_id}' found"
                )
                logger.warning("[%s] %s", ctx.run_id, message)
                state.setdefault("errors", []).append(message)

        candidates: list[dict[str, Any]] = []
        immature: list[dict[str, Any]] = []
        for exp in running:
            assessed = await self._assess(ctx, exp)
            (candidates if assessed["maturity"]["mature"] else immature).append(assessed)

        candidates.sort(key=lambda a: str(a["experiment"].get("created_at") or ""))
        return {"candidates": candidates[: self.max_evaluations], "immature": immature}

    async def _assess(self, ctx: AgentContext, exp: dict[str, Any]) -> dict[str, Any]:
        """Fetch results and run the deterministic maturity gate for one experiment."""
        metric = str((exp.get("primary_metric") or {}).get("event") or "").strip()
        results: dict[str, Any] = {}
        if metric:
            try:
                results = await get_experiment_results(
                    experiment_id=str(exp.get("key") or ""),
                    metric=metric,
                    project_id=ctx.project_id,
                    flag_key=str(exp.get("flag_key") or exp.get("key") or ""),
                )
            except Exception as exc:
                logger.debug("No results for %s: %s", exp.get("key"), exc)
        return {
            "experiment": exp,
            "results": results if isinstance(results, dict) else {},
            "maturity": self._maturity(exp, results if isinstance(results, dict) else {}),
        }

    def _maturity(self, exp: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
        """Deterministic maturity gate — no LLM sees an experiment that fails it.

        Mature when every variant has the sample floor AND the minimum runtime
        elapsed, or earlier when the test is already significant with at least
        half the floor (a sequential-style early boundary).
        """
        reasons: list[str] = []
        metric = str((exp.get("primary_metric") or {}).get("event") or "").strip()
        if not metric:
            reasons.append("no primary metric configured")

        variants = results.get("variants") or []
        users = [v.get("users", 0) for v in variants if isinstance(v, dict)]
        min_users = min(users) if len(users) >= 2 else 0
        if not variants:
            reasons.append("no exposure data yet")
        elif len(users) < 2:
            reasons.append("fewer than two variants have exposures")
        elif min_users < self.min_sample_per_variant:
            reasons.append(
                f"smallest variant has {min_users}/{self.min_sample_per_variant} required users"
            )

        started = _parse_when(exp.get("start_date")) or _parse_when(exp.get("created_at"))
        days_running = (datetime.now(UTC) - started).days if started else 0
        if started is None:
            reasons.append("no start date recorded")
        elif days_running < self.min_runtime_days:
            reasons.append(f"running {days_running}/{self.min_runtime_days} required days")

        significant_early = bool(results.get("is_significant")) and min_users >= (
            self.min_sample_per_variant // 2
        )
        mature = not reasons or significant_early
        return {
            "mature": mature,
            "reasons": [] if mature else reasons,
            "min_variant_users": min_users,
            "days_running": days_running,
            "required_sample_per_variant": self.min_sample_per_variant,
            "required_runtime_days": self.min_runtime_days,
            "significant_early_stop": significant_early,
        }

    # ------------------------------------------------------------------
    # reason
    # ------------------------------------------------------------------

    def build_prompt(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> str | None:
        candidates = working.get("candidates", [])
        if not candidates:
            return None
        payload = [
            {
                "experiment": {
                    key: c["experiment"].get(key)
                    for key in (
                        "key", "flag_key", "description", "variants",
                        "primary_metric", "traffic_percentage", "start_date",
                    )
                },
                "results": c["results"],
                "maturity": c["maturity"],
            }
            for c in candidates
        ]
        return EXPERIMENT_EVALUATION_PROMPT.format(
            experiments=json.dumps(payload, default=str),
            context=working.get("context", ""),
        )

    # ------------------------------------------------------------------
    # act
    # ------------------------------------------------------------------

    async def act(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
        output: Any,
    ) -> dict[str, Any]:
        by_id = {
            str(c["experiment"].get("key") or ""): c for c in working.get("candidates", [])
        }
        verdicts: list[dict[str, Any]] = []

        for item in output:
            experiment_id = str(item.get("experiment_id") or "").strip()
            verdict = str(item.get("verdict") or "").strip()
            entry = by_id.get(experiment_id)
            if entry is None:
                # A verdict for an experiment the model was not given is
                # fabricated — acting on it would stop arbitrary experiments.
                logger.warning(
                    "[%s] Dropping verdict for unknown experiment %r",
                    ctx.run_id, experiment_id,
                )
                continue
            if verdict not in VALID_VERDICTS:
                logger.warning(
                    "[%s] Dropping invalid verdict %r for %s",
                    ctx.run_id, verdict, experiment_id,
                )
                continue
            actions = await self._execute(ctx, state, entry, verdict, item)
            verdicts.append(self._verdict_row(entry, item, verdict, actions))

        # A human-scoped run answers even when the experiment is immature.
        if ctx.target_experiment_id:
            for entry in working.get("immature", []):
                maturity = entry["maturity"]
                verdicts.append(self._verdict_row(
                    entry,
                    {"reasoning": "; ".join(maturity["reasons"]) or "not mature yet"},
                    "immature",
                    {"applied": False},
                ))

        for row in verdicts:
            await self._record(ctx, row)

        counts: dict[str, int] = {}
        for row in verdicts:
            counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
        return {
            "verdicts": verdicts,
            "verdict_counts": counts,
            "evaluated": len(verdicts),
            "needs_approval": False,
        }

    def finalize(self, output: Any, action: dict[str, Any]) -> Any:
        return action.get("verdicts", [])

    # ------------------------------------------------------------------
    # verdict execution
    # ------------------------------------------------------------------

    @staticmethod
    def _verdict_row(
        entry: dict[str, Any], item: dict[str, Any], verdict: str, actions: dict[str, Any]
    ) -> dict[str, Any]:
        exp = entry["experiment"]
        return {
            "experiment_id": str(exp.get("key") or ""),
            "flag_key": str(exp.get("flag_key") or exp.get("key") or ""),
            "verdict": verdict,
            "reasoning": str(item.get("reasoning") or ""),
            "key_numbers": item.get("key_numbers") or {},
            "durable_feature": str(item.get("durable_feature") or ""),
            "results": entry.get("results", {}),
            "actions": actions,
        }

    async def _execute(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        entry: dict[str, Any],
        verdict: str,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply one verdict's side effects. Failures are contained per action —
        a config-service hiccup on one experiment must not lose the sweep."""
        exp = entry["experiment"]
        experiment_id = str(exp.get("key") or "")
        flag_key = str(exp.get("flag_key") or experiment_id)
        actions: dict[str, Any] = {"applied": False}

        async def _try(name: str, coro) -> None:
            try:
                await coro
                actions[name] = True
            except Exception as exc:
                actions[name] = False
                message = f"{name} failed for {experiment_id}: {exc}"
                logger.error("[%s] %s", ctx.run_id, message)
                state.setdefault("errors", []).append(message)

        # L1 is suggest-only: verdicts are recorded and audited, nothing changes.
        # "extend" changes nothing by definition.
        if ctx.autonomy_level <= 1 or verdict == "extend":
            actions["applied"] = verdict == "extend"
        elif verdict == "ship":
            await _try("stopped", update_experiment_status(
                ctx.project_id, experiment_id, "completed"
            ))
            actions["applied"] = True
        elif verdict == "rollback":
            await _try("stopped", update_experiment_status(
                ctx.project_id, experiment_id, "stopped"
            ))
            await _try("flag_disabled", disable_flag(
                ctx.project_id, flag_key,
                evidence={"verdict": "rollback", "key_numbers": item.get("key_numbers") or {}},
            ))
            changeset_id = ""
            if ctx.pool is not None:
                try:
                    row = await get_designed_experiment(ctx.pool, ctx.project_id, experiment_id)
                    changeset_id = str((row or {}).get("changeset_id") or "")
                except Exception as exc:
                    logger.warning("Ledger lookup failed for %s: %s", experiment_id, exc)
            if changeset_id:
                await _try("reverted", revert_changeset(changeset_id))
            actions["applied"] = True
        elif verdict == "iterate":
            await _try("stopped", update_experiment_status(
                ctx.project_id, experiment_id, "stopped"
            ))
            if ctx.pool is not None:
                # Release the source insight so the next design run may
                # redesign it with this learning in memory.
                try:
                    await set_designed_experiment_status(
                        ctx.pool, ctx.project_id, experiment_id, "iterate_requested"
                    )
                    actions["insight_released"] = True
                except Exception as exc:
                    logger.warning("Could not release insight for %s: %s", experiment_id, exc)
            actions["applied"] = True

        await ctx.audit.log(
            ctx.run_id,
            "experiment_verdict",
            {"experiment_id": experiment_id, "verdict": verdict, "actions": actions},
        )
        return actions

    async def _record(self, ctx: AgentContext, row: dict[str, Any]) -> None:
        if ctx.pool is None:
            return
        try:
            await record_verdict(
                ctx.pool,
                ctx.project_id,
                ctx.run_id,
                experiment_id=row["experiment_id"],
                verdict=row["verdict"],
                reasoning=row["reasoning"],
                results={"key_numbers": row["key_numbers"], "stats": row.get("results", {})},
                durable_feature=row["durable_feature"],
            )
        except Exception as exc:
            logger.error("Could not record verdict for %s: %s", row["experiment_id"], exc)

    def memory_entries(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
        output: Any,
        action: dict[str, Any],
    ) -> list[MemoryEntry]:
        return [
            MemoryEntry(
                content=json.dumps(
                    {
                        "experiment_id": row["experiment_id"],
                        "verdict": row["verdict"],
                        "reasoning": row["reasoning"],
                        "key_numbers": row["key_numbers"],
                    },
                    default=str,
                ),
                metadata={
                    "type": "experiment_verdict",
                    "experiment_id": row["experiment_id"],
                    "verdict": row["verdict"],
                },
            )
            for row in action.get("verdicts", [])
        ]
