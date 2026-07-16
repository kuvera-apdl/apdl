"""Atomic lifecycle scheduler for experiment starts and completions."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.store import mutations
from app.store import postgres as pg_store

logger = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 1
MAX_INTERVAL_SECONDS = 86_400


def validate_interval_seconds(interval_seconds: int) -> int:
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, int)
        or not MIN_INTERVAL_SECONDS <= interval_seconds <= MAX_INTERVAL_SECONDS
    ):
        raise ValueError(
            "EXPERIMENT_LIFECYCLE_INTERVAL_SECONDS must be between "
            f"{MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS}"
        )
    return interval_seconds


async def advance_due_experiments(
    pool,
    *,
    now: datetime | None = None,
) -> int:
    """Start scheduled experiments and complete expired experiments."""
    current = now or datetime.now(timezone.utc)
    candidates = await pg_store.get_due_experiments(pool, current)
    advanced = 0
    for experiment in candidates:
        try:
            result = await mutations.transition_due_experiment(
                pool,
                project_id=experiment["project_id"],
                key=experiment["key"],
                expected_version=experiment["version"],
                now=current,
            )
        except Exception:
            logger.exception(
                "Experiment scheduler failed for %s/%s",
                experiment.get("project_id"),
                experiment.get("key"),
            )
            continue
        if result is not None:
            advanced += 1
    if advanced:
        logger.info("Experiment scheduler advanced %d experiment(s)", advanced)
    return advanced


async def run_lifecycle_monitor(
    pool,
    *,
    interval_seconds: int,
) -> None:
    """Continuously run the atomic lifecycle scheduler."""
    interval_seconds = validate_interval_seconds(interval_seconds)
    while True:
        try:
            await advance_due_experiments(pool)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Experiment lifecycle sweep failed")
        await asyncio.sleep(interval_seconds)
