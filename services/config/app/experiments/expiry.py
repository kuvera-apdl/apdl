"""Time-based experiment expiry sweep.

Nothing in the request path consults an experiment's ``end_date``: a status is
only ever advanced by an explicit admin / API / agent edit, so an experiment
keeps ``running`` (and its backing flag keeps serving) forever once its end date
passes. This module is the missing scheduler.

It periodically completes any ``running`` experiment whose ``end_date`` is in the
past and cascades to the backing flag exactly as a manual "mark completed" would
(``experiment_flag.status_to_flag_state("completed") -> ("disabled", False)``,
mirroring the ``update_experiment`` admin endpoint). Flags carry no end date of
their own; an experiment's flag is disabled by completing the experiment.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone

from app.flags import experiment_flag
from app.models.schemas import GateRule, VariantConfig
from app.store import postgres as pg_store
from app.store import redis_cache
from app.utils import serialize_client_flag

logger = logging.getLogger(__name__)

# Recorded on the cascaded flag audit entry so the trail shows the system, not a
# human, disabled the flag, and why.
EXPIRY_ACTOR = "system"
EXPIRY_REASON = "experiment_ended"


def parse_end_date(raw) -> date | None:
    """Parse a stored ``end_date`` (free-text TEXT) into a date, else None.

    Accepts ``YYYY-MM-DD`` (what the console writes) and full ISO-8601 datetimes
    (which the API also accepts). Anything unparseable is treated as "no end
    date" so a typo can never silently disable an experiment.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return date.fromisoformat(candidate[:10])
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(candidate).date()
    except ValueError:
        logger.warning("expiry: ignoring unparseable end_date %r", raw)
        return None


def is_expired(raw, today: date) -> bool:
    """True when ``end_date`` is set and strictly before ``today``.

    The end date is inclusive: an experiment ending 2026-06-01 still serves that
    day and is completed on 2026-06-02.
    """
    end = parse_end_date(raw)
    return end is not None and end < today


def _load_json(raw, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _projected_variants(variants_json: str) -> list[VariantConfig]:
    return [
        VariantConfig(key=v["key"], weight=int(v["weight"]))
        for v in _load_json(variants_json, [])
    ]


def _experiment_rules(targeting_rules_json: str) -> list[GateRule]:
    return [GateRule.model_validate(r) for r in _load_json(targeting_rules_json, [])]


async def _broadcast_flag(broadcaster, project_id: str, flag: dict) -> None:
    """Mirror admin._broadcast_flag_change for a completed-experiment flag."""
    if broadcaster is None:
        return
    if flag.get("evaluation_mode") in {"client", "both"} and not flag.get("archived_at"):
        payload = {"action": "flag_updated", "flag": serialize_client_flag(flag)}
    else:
        payload = {"action": "flag_removed", "key": flag["key"]}
    await broadcaster.broadcast(project_id, "flag_update", json.dumps(payload, separators=(",", ":")))


async def _complete_experiment(pool, redis, broadcaster, exp: dict) -> bool:
    """Complete one expired experiment and disable its backing flag."""
    project_id = exp["project_id"]
    key = exp["key"]
    flag_key = exp.get("flag_key") or key

    completed = dict(exp)
    completed["status"] = "completed"

    # Disable the backing flag FIRST, then persist the experiment — mirroring the
    # admin update_experiment endpoint, "so a stored experiment never describes a
    # flag that failed to apply". The old order (persist completed, then disable)
    # could leave an experiment 'completed' while its flag kept serving: a
    # concurrent flag edit makes update_flag return None, and since the
    # experiment is no longer 'running' the sweep never retries → permanent leak.
    backing = await pg_store.get_flag(pool, project_id, flag_key)
    if backing is not None:
        flag_update = experiment_flag.build_flag_update(
            version=backing["version"],
            flag_key=flag_key,
            name=backing.get("name") or flag_key,
            description=completed.get("description", ""),
            status="completed",
            variants=_projected_variants(completed.get("variants_json", "[]")),
            default_variant=completed.get("default_variant", "control"),
            traffic_percentage=float(completed.get("traffic_percentage", 100.0)),
            targeting_rules=_experiment_rules(completed.get("targeting_rules_json", "[]")),
        )
        updates = flag_update.model_dump(exclude_unset=True, exclude_none=True, mode="json")
        updates.pop("version", None)
        merged = {**backing, **updates}
        updated = await pg_store.update_flag(pool, merged, backing["version"])
        if updated is None:
            # Optimistic-version mismatch from a concurrent edit. Leave the
            # experiment 'running' and bail so the next sweep retries the whole
            # cascade — never mark it completed with a still-enabled flag.
            logger.warning(
                "expiry: backing flag %s/%s changed concurrently; will retry next sweep",
                project_id,
                flag_key,
            )
            return False
        await pg_store.create_flag_audit_entry(
            pool,
            project_id=project_id,
            flag_key=flag_key,
            action="flag_disabled",
            actor=EXPIRY_ACTOR,
            before=backing,
            after=updated,
            reason=EXPIRY_REASON,
        )
        await redis_cache.invalidate_flags(redis, project_id)
        await _broadcast_flag(broadcaster, project_id, updated)

    if not await pg_store.update_experiment(pool, completed):
        logger.error("expiry: failed to persist completion for %s/%s", project_id, key)
        return False

    await redis_cache.invalidate_experiments(redis, project_id)
    if broadcaster is not None:
        await broadcaster.broadcast(
            project_id,
            "experiment_update",
            json.dumps(
                {"action": "experiment_updated", "key": key, "status": "completed", "flag_key": flag_key},
                separators=(",", ":"),
            ),
        )
    logger.info(
        "expiry: completed experiment %s/%s (end_date %s); backing flag '%s' disabled",
        project_id,
        key,
        exp.get("end_date"),
        flag_key,
    )
    return True


async def expire_due_experiments(pool, redis, broadcaster, *, today: date | None = None) -> int:
    """Run a single sweep. Returns the number of experiments completed."""
    if today is None:
        today = datetime.now(timezone.utc).date()
    candidates = await pg_store.get_running_experiments_with_end_date(pool)
    completed = 0
    for exp in candidates:
        if not is_expired(exp.get("end_date"), today):
            continue
        try:
            if await _complete_experiment(pool, redis, broadcaster, exp):
                completed += 1
        except Exception as exc:  # one bad experiment must not stall the sweep
            logger.exception(
                "expiry: error completing %s/%s: %s",
                exp.get("project_id"),
                exp.get("key"),
                exc,
            )
    if completed:
        logger.info("expiry: completed %d expired experiment(s)", completed)
    return completed


async def run_expiry_monitor(pool, redis, broadcaster, *, interval_seconds: int) -> None:
    """Continuously expire experiments whose end date has passed."""
    while True:
        try:
            await expire_due_experiments(pool, redis, broadcaster)
        except Exception as exc:
            logger.exception("expiry: sweep failed: %s", exc)
        await asyncio.sleep(interval_seconds)
