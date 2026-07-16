"""GET /v1/flags endpoint -- SDK polling for flag configuration."""

import json
import logging

from fastapi import APIRouter, Request, Response

from app.auth import authorized_project
from app.store import postgres as pg_store
from app.store import redis_cache
from app.utils import serialize_flag_collection

logger = logging.getLogger(__name__)

router = APIRouter()


def _flags_to_json(project_id: str, flags: list[dict]) -> str:
    """Serialize flags to the canonical SDK bootstrap payload."""
    return json.dumps(
        serialize_flag_collection(project_id, flags), separators=(",", ":")
    )


@router.get("/v1/flags")
async def get_flags(request: Request):
    """Return all flags for a project. Checks Redis cache first."""
    project_id = authorized_project(request, "config:read")

    redis = request.app.state.redis

    # Check Redis cache first
    cached = await redis_cache.get_flags(redis, project_id)
    if cached is not None:
        logger.debug("Cache hit for flags of project %s", project_id)
        return Response(
            content=cached.config_json,
            media_type="application/json",
            headers={"X-Cache": "HIT"},
        )

    # Cache miss -- query PostgreSQL
    logger.debug("Cache miss for flags of project %s, querying Postgres", project_id)
    pool = request.app.state.pg_pool
    flags, project_version = await pg_store.get_flag_snapshot(
        pool,
        project_id,
        client_visible_only=True,
    )
    flags_json = _flags_to_json(project_id, flags)

    cached_snapshot = await redis_cache.set_flags(
        redis,
        project_id,
        project_version,
        flags_json,
        ttl=60,
    )
    if not cached_snapshot:
        # An invalidation raced this miss after its database snapshot. Refetch
        # once; never loop indefinitely under sustained mutation traffic.
        flags, project_version = await pg_store.get_flag_snapshot(
            pool,
            project_id,
            client_visible_only=True,
        )
        flags_json = _flags_to_json(project_id, flags)
        await redis_cache.set_flags(
            redis,
            project_id,
            project_version,
            flags_json,
            ttl=60,
        )

    return Response(
        content=flags_json,
        media_type="application/json",
        headers={"X-Cache": "MISS"},
    )
