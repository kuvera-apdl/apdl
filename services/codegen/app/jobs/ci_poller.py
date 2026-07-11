"""Periodic GitHub PR and CI recovery for missed webhook observations."""

from __future__ import annotations

import asyncio
import logging

import asyncpg

from app.jobs.ci import (
    CIEvidenceReader,
    CIFailureHandler,
    PullRequestReader,
    TokenMinter,
    sync_github_state,
)
from app.store import changesets as store

logger = logging.getLogger(__name__)


async def poll_github_once(
    pool: asyncpg.Pool,
    *,
    get_pull_request: PullRequestReader,
    get_ci_evidence: CIEvidenceReader,
    mint_token: TokenMinter,
    repair_failure: CIFailureHandler | None = None,
) -> int:
    """Recover every open PR regardless of its current CI projection or age."""
    ids = await store.list_syncable_changeset_ids(pool)
    for changeset_id in ids:
        try:
            await sync_github_state(
                pool,
                changeset_id,
                get_pull_request=get_pull_request,
                get_ci_evidence=get_ci_evidence,
                mint_token=mint_token,
                repair_failure=repair_failure,
            )
        except Exception:
            logger.warning(
                "GitHub recovery failed for changeset %s",
                changeset_id,
                exc_info=True,
            )
    return len(ids)


async def run_github_poller(
    pool: asyncpg.Pool,
    *,
    interval_seconds: int,
    get_pull_request: PullRequestReader,
    get_ci_evidence: CIEvidenceReader,
    mint_token: TokenMinter,
    repair_failure: CIFailureHandler | None = None,
) -> None:
    """Continuously recover GitHub state until service shutdown."""
    logger.info("GitHub recovery poller started (interval=%ss)", interval_seconds)
    try:
        while True:
            try:
                await poll_github_once(
                    pool,
                    get_pull_request=get_pull_request,
                    get_ci_evidence=get_ci_evidence,
                    mint_token=mint_token,
                    repair_failure=repair_failure,
                )
            except Exception:
                logger.warning(
                    "GitHub recovery sweep errored; retrying next interval",
                    exc_info=True,
                )
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        logger.info("GitHub recovery poller stopped")
        raise
