"""GET /v1/flags endpoint -- SDK polling for flag configuration."""

import json
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.store import postgres as pg_store
from app.store import redis_cache
from app.utils import extract_project_id, serialize_flag_collection

logger = logging.getLogger(__name__)

router = APIRouter()


def _flags_to_json(project_id: str, flags: list[dict]) -> str:
    """Serialize flags to the canonical SDK bootstrap payload."""
    return json.dumps(serialize_flag_collection(project_id, flags), separators=(",", ":"))


@router.get("/v1/flags")
async def get_flags(request: Request):
    """Return all flags for a project. Checks Redis cache first."""
    project_id = extract_project_id(request)
    if not project_id:
        return JSONResponse(
            status_code=401,
            content={
                "error": "unauthorized",
                "message": "API key or project_id required",
            },
        )

    redis = request.app.state.redis

    # Check Redis cache first
    cached = await redis_cache.get_flags(redis, project_id)
    if cached is not None:
        logger.debug("Cache hit for flags of project %s", project_id)
        return Response(
            content=cached,
            media_type="application/json",
            headers={"X-Cache": "HIT"},
        )

    # Cache miss -- query PostgreSQL
    logger.debug(
        "Cache miss for flags of project %s, querying Postgres", project_id
    )
    pool = request.app.state.pg_pool
    flags = await pg_store.get_flags(pool, project_id, client_exposed_only=True)

    flags_json = _flags_to_json(project_id, flags)

    # Populate cache
    await redis_cache.set_flags(redis, project_id, flags_json, ttl=60)

    return Response(
        content=flags_json,
        media_type="application/json",
        headers={"X-Cache": "MISS"},
    )
