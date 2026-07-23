"""Trusted server-side feature flag evaluation."""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.responses import Response

from app.auth import require_project
from app.flags.evaluator import evaluate as evaluate_gate
from app.models.schemas import GateEvaluateRequest, GateEvaluateResponse
from app.store import mutations
from app.store import postgres as pg_store

logger = logging.getLogger(__name__)

FEATURE_FLAG_EXPOSURE_EVENT = "$feature_flag_exposure"
SERVER_EXPOSURE_SOURCE = "server"
MAX_EVALUATE_REQUEST_BYTES = 65_536


class _RequestBodyTooLarge(Exception):
    pass


async def _read_bounded_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_EVALUATE_REQUEST_BYTES:
            raise _RequestBodyTooLarge
        body.extend(chunk)
    return bytes(body)


def _invalid_content_length_response() -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "bad_request", "message": "Invalid Content-Length"},
    )


class BoundedEvaluateRoute(APIRoute):
    """Bound raw JSON bytes before FastAPI performs model validation."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        route_handler = super().get_route_handler()

        async def bounded_route_handler(request: Request) -> Response:
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    parsed_length = int(content_length)
                except ValueError:
                    return _invalid_content_length_response()
                if parsed_length < 0:
                    return _invalid_content_length_response()
                if parsed_length > MAX_EVALUATE_REQUEST_BYTES:
                    return _payload_too_large_response()
            try:
                raw_body = await _read_bounded_body(request)
            except _RequestBodyTooLarge:
                return _payload_too_large_response()
            except Exception:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "bad_request",
                        "message": "Could not read request body",
                    },
                )

            # FastAPI will read the same bounded bytes from Request.body() and
            # retain its standard OpenAPI and validation-error behavior.
            setattr(request, "_body", raw_body)
            return await route_handler(request)

        return bounded_route_handler


router = APIRouter(route_class=BoundedEvaluateRoute)


@router.post("/v1/evaluate", response_model=GateEvaluateResponse)
async def evaluate(body: GateEvaluateRequest, request: Request):
    """Evaluate a server-side flag without exposing rules to browser clients."""
    require_project(request, body.project_id, "config:evaluate")

    if (
        body.log_exposure
        and not body.context.user_id
        and not body.context.anonymous_id
    ):
        return JSONResponse(
            status_code=422,
            content={
                "error": "identity_required",
                "message": (
                    "log_exposure requires context.user_id or "
                    "context.anonymous_id"
                ),
            },
        )

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

    # Presence operators distinguish an omitted identity from an explicitly
    # supplied empty string. Preserve that distinction instead of materializing
    # EvalContext's convenience defaults into keys that were absent on input.
    evaluator_context = body.context.model_dump(mode="json", exclude_unset=True)
    result = evaluate_gate(flag, evaluator_context)
    response = GateEvaluateResponse(**{**result, "source": SERVER_EXPOSURE_SOURCE})

    if response.reason == "invalid_config":
        logger.error(
            "Stored flag configuration failed canonical validation",
            extra={
                "event": "flag_configuration_invalid",
                "project_id": body.project_id,
                "flag_key": body.key,
                "config_version": response.config_version,
            },
        )

    if body.log_exposure and response.variant is not None:
        try:
            await _enqueue_exposure(request, body, response)
        except mutations.IntegrityError as exc:
            return JSONResponse(
                status_code=409,
                content={"error": "message_id_conflict", "message": str(exc)},
            )
        except Exception:
            logger.exception("Failed to persist server-side exposure intent")
            return JSONResponse(
                status_code=503,
                content={
                    "error": "exposure_persistence_unavailable",
                    "message": "The assignment was not returned or applied",
                },
            )

    return response


def _payload_too_large_response() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "error": "payload_too_large",
            "message": (
                f"Request body exceeds {MAX_EVALUATE_REQUEST_BYTES} bytes"
            ),
        },
    )


async def _enqueue_exposure(
    request: Request,
    body: GateEvaluateRequest,
    result: GateEvaluateResponse,
) -> None:
    user_id = body.context.user_id
    anonymous_id = body.context.anonymous_id
    message_id = body.message_id
    session_id = body.session_id or f"server:{message_id}"
    timestamp = _timestamp()
    event: dict = {
        "event": FEATURE_FLAG_EXPOSURE_EVENT,
        "type": "track",
        "timestamp": timestamp,
        "message_id": message_id,
        "session_id": session_id,
        "context": {
            "library": {
                "name": "apdl-config",
                "version": "server",
            },
        },
        "properties": {
            "flag_key": result.key,
            "variant": result.variant,
            "reason": result.reason,
            "rule_id": result.rule_id,
            "rollout_bucket": result.rollout_bucket,
            "variant_bucket": result.variant_bucket,
            "rollout_percentage": result.rollout_percentage,
            "bucket_by": result.bucket_by,
            "config_version": result.config_version,
            "source": SERVER_EXPOSURE_SOURCE,
            "page": body.page,
            "component": body.component,
        },
    }
    if user_id:
        event["user_id"] = user_id
    if anonymous_id:
        event["anonymous_id"] = anonymous_id

    stream_key = f"events:raw:{body.project_id}"
    await mutations.enqueue_exposure(
        request.app.state.pg_pool,
        project_id=body.project_id,
        message_id=message_id,
        stream_key=stream_key,
        event=event,
    )


def _timestamp() -> str:
    now = datetime.now(timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
