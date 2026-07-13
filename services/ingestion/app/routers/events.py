"""POST /v1/events handler -- core ingestion route.

Ported 1:1 from the C++ handle_events() in src/handlers/events.cpp.
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

from app.auth import require_role
from app.middleware.rate_limit import check_rate_limit
from app.privacy import sanitize_auto_capture_events
from app.streaming.redis_producer import publish_event
from app.validation.schema import validate_event_batch

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/v1/events", status_code=202)
async def ingest_events(request: Request):
    principal = require_role(request, "events:write")
    project_id = principal.project_id

    # Rate limit
    rate_result = await check_rate_limit(project_id, request)
    if rate_result is not None:
        return rate_result

    # Parse body
    try:
        body = await request.json()
    except Exception:
        return Response(
            content=json.dumps({
                "error": "bad_request",
                "message": "Invalid JSON in request body",
            }),
            status_code=400,
            media_type="application/json",
        )

    body = sanitize_auto_capture_events(body)

    if not body:
        return Response(
            content=json.dumps({
                "error": "bad_request",
                "message": "Request body is empty",
            }),
            status_code=400,
            media_type="application/json",
        )

    # Validate
    validation = validate_event_batch(body)
    if not validation["valid"]:
        return Response(
            content=json.dumps({
                "error": "validation_failed",
                "errors": validation["errors"],
            }),
            status_code=400,
            media_type="application/json",
        )

    events = body["events"]
    now = datetime.now(timezone.utc)
    server_ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or request.headers.get("x-real-ip", "")
        or (request.client.host if request.client else "")
    )
    stream_key = f"events:raw:{project_id}"

    accepted = 0
    failed = 0
    redis = request.app.state.redis

    for event in events:
        event["server_timestamp"] = server_ts
        event["ip"] = client_ip
        event["project_id"] = project_id

        try:
            await publish_event(redis, stream_key, event)
            accepted += 1
        except Exception:
            failed += 1
            logger.warning(
                "Failed to publish event to Redis stream %s", stream_key
            )

    if accepted == 0 and failed > 0:
        return Response(
            content=json.dumps({
                "error": "service_unavailable",
                "message": "Failed to enqueue events to processing pipeline",
            }),
            status_code=503,
            media_type="application/json",
        )

    result = {"accepted": accepted}
    if failed > 0:
        result["failed"] = failed

    logger.info(
        "Ingested %d events for project %s (%d failed)",
        accepted,
        project_id,
        failed,
    )
    return Response(
        content=json.dumps(result),
        status_code=202,
        media_type="application/json",
    )
