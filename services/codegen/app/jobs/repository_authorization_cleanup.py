"""Periodic retention cleanup for short-lived GitHub authorization evidence."""

from __future__ import annotations

import asyncio
import logging

import asyncpg

from app.store.repository_authorizations import purge_expired_authorizations

logger = logging.getLogger(__name__)

REPOSITORY_AUTHORIZATION_CLEANUP_INTERVAL_SECONDS = 60.0


async def run_repository_authorization_cleanup(
    pool: asyncpg.Pool,
    *,
    interval_seconds: float = REPOSITORY_AUTHORIZATION_CLEANUP_INTERVAL_SECONDS,
) -> None:
    """Continuously purge bounded batches, without relying on a future start."""
    while True:
        try:
            await purge_expired_authorizations(pool)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("GitHub repository authorization cleanup failed")
        await asyncio.sleep(interval_seconds)
