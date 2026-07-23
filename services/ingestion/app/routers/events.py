"""POST /v1/events handler -- core ingestion route.

Ported 1:1 from the C++ handle_events() in src/handlers/events.cpp.
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

from app.auth import require_role
from app.client_ip import client_ip
from app.middleware.rate_limit import admit_bytes, admit_events, admit_request
from app.privacy import sanitize_auto_capture_events
from app.request_body_limit import (
    PAYLOAD_TOO_LARGE_CONTENT,
    RequestBodyTooLarge,
)
from app.streaming.redis_producer import (
    EVENT_STREAM_RETRY_AFTER_SECONDS,
    StreamOverloaded,
    publish_batch,
)
from app.validation.json_contract import (
    MAX_REQUEST_BYTES,
    CanonicalJSONError,
    parse_canonical_json,
    validate_event_json_bounds,
)
from app.validation.schema import validate_event_batch

logger = logging.getLogger(__name__)

router = APIRouter()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _read_bounded_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            raise RequestBodyTooLarge
        body.extend(chunk)
    return bytes(body)


@router.post("/v1/events", status_code=202)
async def ingest_events(request: Request):
    principal = require_role(request, "events:write")
    project_id = principal.project_id
    redis = request.app.state.redis
    received_at = _utc_now()
    received_at = received_at.replace(
        microsecond=(received_at.microsecond // 1_000) * 1_000
    )

    # Charge authenticated traffic before Content-Length checks, body reads,
    # privacy processing, or schema work. Invalid requests consume this budget.
    rate_result = await admit_request(redis, principal, request)
    if rate_result is not None:
        return rate_result

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return _error_response(
                    413,
                    PAYLOAD_TOO_LARGE_CONTENT["error"],
                    PAYLOAD_TOO_LARGE_CONTENT["message"],
                )
        except ValueError:
            return _error_response(400, "bad_request", "Invalid Content-Length")

    try:
        raw_body = await _read_bounded_body(request)
    except RequestBodyTooLarge:
        return _error_response(
            413,
            PAYLOAD_TOO_LARGE_CONTENT["error"],
            PAYLOAD_TOO_LARGE_CONTENT["message"],
        )
    except Exception:
        return _error_response(400, "bad_request", "Could not read request body")

    # Preserve byte-proportional abuse protection after the hard body bound,
    # but before JSON parsing, privacy processing, or schema validation.
    rate_result = await admit_bytes(redis, principal, request, len(raw_body))
    if rate_result is not None:
        return rate_result

    try:
        body = parse_canonical_json(raw_body)
    except CanonicalJSONError as exc:
        return _error_response(400, "bad_request", str(exc))

    if isinstance(body, dict) and isinstance(body.get("events"), list):
        try:
            for event in body["events"]:
                validate_event_json_bounds(event)
        except CanonicalJSONError as exc:
            return Response(
                content=json.dumps({
                    "error": "validation_failed",
                    "errors": [{"field": "events", "message": str(exc)}],
                }),
                status_code=400,
                media_type="application/json",
            )

    body = sanitize_auto_capture_events(body)

    if not body:
        return _error_response(400, "bad_request", "Request body is empty")

    # Validate
    validation = validate_event_batch(body, received_at=received_at)
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
    rate_result = await admit_events(redis, principal, request, events)
    if rate_result is not None:
        return rate_result

    server_ts = received_at.strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{received_at.microsecond // 1000:03d}Z"
    )
    source_ip = client_ip(request)
    stream_key = f"events:raw:{project_id}"

    for event in events:
        event["server_timestamp"] = server_ts
        event["ip"] = source_ip
        event["project_id"] = project_id

    try:
        await publish_batch(redis, stream_key, events)
    except StreamOverloaded:
        return _error_response(
            503,
            "service_overloaded",
            "Event persistence backlog is at capacity",
            headers={"Retry-After": str(EVENT_STREAM_RETRY_AFTER_SECONDS)},
        )
    except Exception:
        logger.warning(
            "Atomic Redis publish failed for stream %s; client must retry stable IDs",
            stream_key,
        )
        return _error_response(
            503,
            "service_unavailable",
            "Failed to atomically enqueue event batch",
        )

    logger.info(
        "Ingested %d events for project %s",
        len(events),
        project_id,
    )
    return Response(
        content=json.dumps({"accepted": len(events)}),
        status_code=202,
        media_type="application/json",
    )


def _error_response(
    status_code: int,
    error: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    return Response(
        content=json.dumps({"error": error, "message": message}),
        status_code=status_code,
        media_type="application/json",
        headers=headers,
    )
