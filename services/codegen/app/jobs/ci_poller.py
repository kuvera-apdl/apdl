"""Periodic CI-status poller — the webhook-free default trigger.

A self-hosted codegen rarely has a public HTTPS endpoint for GitHub to deliver
webhooks to (it may sit behind NAT, on a laptop, or be reachable only by an SSH
tunnel). Rather than make the merge queue depend on inbound connectivity, this
poller drives CI sync from the outside in: every interval it sweeps the
changesets a sync can still advance and runs :func:`sync_ci_status` on each,
reusing the very same injected deps as the webhook path (``app.state.ci_deps``).

A repo with no CI resolves to ``none`` and unblocks merge within one interval; a
repo with CI advances as its checks report. The GitHub webhook remains an
optional lower-latency accelerator — not a requirement. Set the interval to 0
(``CODEGEN_CI_POLL_INTERVAL=0``) to disable polling when webhooks are wired.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

from app.jobs.ci import CIStatusReader, ReadyMarker, TokenMinter, sync_ci_status
from app.store import changesets as store

logger = logging.getLogger(__name__)


async def poll_ci_once(
    pool: asyncpg.Pool,
    *,
    get_status: CIStatusReader,
    mint_token: TokenMinter,
    mark_ready: ReadyMarker | None = None,
) -> int:
    """Run a single CI-sync sweep over every syncable changeset.

    Each changeset is synced independently — one failure is logged and skipped so
    it can't stall the rest of the sweep. Returns the number of changesets swept.
    """
    ids = await store.list_syncable_changeset_ids(pool)
    for changeset_id in ids:
        try:
            await sync_ci_status(
                pool,
                changeset_id,
                get_status=get_status,
                mint_token=mint_token,
                mark_ready=mark_ready,
            )
        except Exception:
            logger.warning("CI poll failed for changeset %s", changeset_id, exc_info=True)
    return len(ids)


async def run_ci_poller(
    pool: asyncpg.Pool,
    *,
    interval_seconds: int,
    get_status: CIStatusReader,
    mint_token: TokenMinter,
    mark_ready: ReadyMarker | None = None,
) -> None:
    """Sweep CI status forever, every ``interval_seconds``, until cancelled.

    A sweep that raises is logged and retried on the next tick — the loop never
    dies on a transient GitHub/database error. Cancellation (on shutdown) exits
    cleanly.
    """
    logger.info("CI poller started (interval=%ss)", interval_seconds)
    try:
        while True:
            try:
                swept = await poll_ci_once(
                    pool,
                    get_status=get_status,
                    mint_token=mint_token,
                    mark_ready=mark_ready,
                )
                if swept:
                    logger.debug("CI poll swept %d changeset(s)", swept)
            except Exception:
                logger.warning("CI poll sweep errored; retrying next interval", exc_info=True)
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        logger.info("CI poller stopped")
        raise
