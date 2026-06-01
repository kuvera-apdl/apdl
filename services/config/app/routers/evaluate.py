"""Trusted server-side feature gate evaluation."""

import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.flags.evaluator import evaluate as evaluate_gate
from app.models.schemas import GateEvaluateRequest, GateEvaluateResponse
from app.store import postgres as pg_store

logger = logging.getLogger(__name__)

router = APIRouter()

FEATURE_FLAG_EXPOSURE_EVENT = "$feature_flag_exposure"
SERVER_EXPOSURE_SOURCE = "server"
STREAM_MAXLEN = 1000000


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": "unauthorized", "message": "Valid internal token required"},
    )


def _is_trusted_request(request: Request) -> bool:
    expected = os.environ.get("APDL_INTERNAL_TOKEN", "")
    provided = request.headers.get("x-apdl-internal-token", "")
    return bool(expected) and secrets.compare_digest(provided, expected)


@router.post("/v1/evaluate", response_model=GateEvaluateResponse)
async def evaluate(body: GateEvaluateRequest, request: Request):
    """Evaluate a server-side gate without exposing rules to browser clients."""
    if not _is_trusted_request(request):
        return _unauthorized()

    flag = await pg_store.get_flag(
        request.app.state.pg_pool,
        body.project_id,
        body.key,
    )
    if flag is None:
        return GateEvaluateResponse(key=body.key, reason="not_found")

    if flag.get("evaluation_mode") == "client":
        return JSONResponse(
            status_code=403,
            content={
                "error": "invalid_evaluation_mode",
                "message": f"Flag '{body.key}' is not enabled for server-side evaluation",
            },
        )

    result = evaluate_gate(flag, body.context.model_dump(mode="json"))
    response = GateEvaluateResponse(**result, source=SERVER_EXPOSURE_SOURCE)

    if body.log_exposure and result["reason"] != "not_found":
        await _publish_exposure(request, body, response)

    return response


async def _publish_exposure(
    request: Request,
    body: GateEvaluateRequest,
    result: GateEvaluateResponse,
) -> None:
    user_id = body.context.user_id
    anonymous_id = body.context.anonymous_id
    if not user_id and not anonymous_id:
        return

    message_id = body.message_id or f"srv_{uuid.uuid4()}"
    session_id = body.session_id or f"server:{message_id}"
    timestamp = _timestamp()
    event: dict = {
        "event": FEATURE_FLAG_EXPOSURE_EVENT,
        "type": "track",
        "timestamp": timestamp,
        "message_id": message_id,
        "session_id": session_id,
        "properties": {
            "flag_key": result.key,
            "value": result.value,
            "reason": result.reason,
            "rule_id": result.rule_id,
            "bucket": result.bucket,
            "rollout_percentage": result.rollout_percentage,
            "bucket_by": result.bucket_by,
            "config_version": result.config_version,
            "source": SERVER_EXPOSURE_SOURCE,
            "page": body.page,
        },
    }
    if user_id:
        event["user_id"] = user_id
    if anonymous_id:
        event["anonymous_id"] = anonymous_id

    stream_key = f"events:raw:{body.project_id}"
    try:
        await request.app.state.redis.xadd(
            stream_key,
            {"event_json": json.dumps(event, separators=(",", ":"))},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as exc:
        logger.warning(
            "Failed to publish server-side exposure for flag %s: %s",
            result.key,
            exc,
        )


def _timestamp() -> str:
    now = datetime.now(timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
