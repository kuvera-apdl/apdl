"""GET /v1/stream endpoint -- SSE for real-time flag updates."""

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.store import postgres as pg_store
from app.utils import extract_project_id, serialize_flag_collection

logger = logging.getLogger(__name__)

router = APIRouter()


def _flags_to_json(project_id: str, flags: list[dict]) -> str:
    """Serialize flags to the canonical SDK bootstrap payload."""
    return json.dumps(serialize_flag_collection(project_id, flags), separators=(",", ":"))


@router.get("/v1/stream")
async def sse_stream(request: Request):
    """SSE endpoint for real-time flag configuration updates."""
    project_id = extract_project_id(request)
    if not project_id:
        return JSONResponse(
            status_code=401,
            content={
                "error": "unauthorized",
                "message": "API key or project_id required for SSE stream",
            },
        )

    pool = request.app.state.pg_pool
    broadcaster = request.app.state.broadcaster

    # Get initial flags from PostgreSQL
    flags = await pg_store.get_flags(pool, project_id, client_visible_only=True)
    initial_data = _flags_to_json(project_id, flags)

    # Create a queue for this connection
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)

    # Register with broadcaster
    conn_id = await broadcaster.add_connection(project_id, queue)

    logger.info(
        "SSE connection %s registered for project %s (total: %d)",
        conn_id,
        project_id,
        await broadcaster.connection_count(project_id),
    )

    async def event_generator():
        try:
            # Send initial config event
            yield f"event: config\ndata: {initial_data}\n\n"

            # Stream subsequent events from the queue
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=35)
                    yield message
                except asyncio.TimeoutError:
                    # Send a heartbeat if nothing in the queue for 35s
                    yield ": heartbeat\n\n"
                except asyncio.CancelledError:
                    break
        finally:
            await broadcaster.remove_connection(project_id, conn_id)
            logger.debug(
                "SSE connection %s disconnected for project %s",
                conn_id,
                project_id,
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )
