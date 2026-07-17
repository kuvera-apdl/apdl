"""GET /v1/stream endpoint -- SSE for real-time flag updates."""

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request, status
from sse_starlette import EventSourceResponse, ServerSentEvent

from app.auth import Principal, authorized_project, credential_has_current_role
from app.client_ip import client_ip
from app.sse.broadcaster import (
    ConnectionQuotaExceeded,
    CloseReason,
    ProjectEvent,
    SSEBroadcaster,
    SSESubscription,
    stream_close_event,
)
from app.store import postgres as pg_store
from app.utils import serialize_flag_collection

logger = logging.getLogger(__name__)

router = APIRouter()


def _flags_to_json(project_id: str, flags: list[dict]) -> str:
    """Serialize flags to the canonical SDK bootstrap payload."""
    return json.dumps(
        serialize_flag_collection(project_id, flags), separators=(",", ":")
    )


@router.get("/v1/stream")
async def sse_stream(request: Request):
    """SSE endpoint for real-time flag configuration updates."""
    project_id = authorized_project(request, "config:read")

    broadcaster = request.app.state.broadcaster
    principal = request.state.principal
    try:
        subscription = await broadcaster.add_connection(
            project_id,
            principal.credential_id,
            client_ip(request),
        )
    except ConnectionQuotaExceeded as exc:
        logger.warning(
            "SSE connection admission rejected",
            extra={
                "event": "sse_connection_rejected",
                "scope": exc.scope,
                "limit": exc.limit,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "sse_connection_limit",
                "scope": exc.scope,
            },
            headers={"Retry-After": "5"},
        ) from exc

    try:
        flags, snapshot_version = await pg_store.get_flag_snapshot(
            request.app.state.pg_pool,
            project_id,
            client_visible_only=True,
        )
        initial_data = _flags_to_json(project_id, flags)
    except Exception:
        await broadcaster.remove_connection(subscription)
        raise

    logger.info(
        "SSE connection %s registered for project %s (total: %d)",
        subscription.connection_id,
        project_id,
        await broadcaster.connection_count(project_id),
    )

    settings = broadcaster.settings
    return EventSourceResponse(
        _event_generator(
            broadcaster,
            subscription,
            pg_pool=request.app.state.pg_pool,
            principal=principal,
            credential_check_interval_seconds=settings.credential_check_interval_seconds,
            initial_data=initial_data,
            snapshot_version=snapshot_version,
        ),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
        ping=settings.ping_interval_seconds,
        ping_message_factory=lambda: ServerSentEvent(
            event="heartbeat",
            data="{}",
        ),
        send_timeout=settings.send_timeout_seconds,
    )


async def _event_generator(
    broadcaster: SSEBroadcaster,
    subscription: SSESubscription,
    *,
    pg_pool,
    principal: Principal,
    credential_check_interval_seconds: float,
    initial_data: str,
    snapshot_version: int,
):
    """Emit a project sync barrier, then only updates newer than its cursor.

    The ``config`` event contains the complete SDK-visible flag snapshot. It is
    also the project-wide reconciliation barrier for non-flag consumers, which
    must refetch their current state when they observe it.
    """
    update_task: asyncio.Task | None = None
    authority_timer: asyncio.Task | None = None
    terminal_reason: CloseReason | None = None
    try:
        try:
            authorized = await credential_has_current_role(
                pg_pool,
                principal,
                "config:read",
            )
        except Exception:
            logger.exception(
                "SSE credential revalidation failed for credential %s",
                principal.credential_id,
            )
            terminal_reason = "credential_authority_unavailable"
            yield stream_close_event(terminal_reason)
            return
        if not authorized:
            terminal_reason = "credential_revoked"
            yield stream_close_event(terminal_reason)
            return

        yield ServerSentEvent(
            event="config",
            data=initial_data,
            id=str(snapshot_version),
        )
        update_task = asyncio.create_task(_next_update(subscription))
        authority_timer = asyncio.create_task(
            asyncio.sleep(credential_check_interval_seconds)
        )
        while True:
            done, _ = await asyncio.wait(
                {update_task, authority_timer},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Credential authority wins a same-tick race with queued updates.
            if authority_timer in done:
                try:
                    authorized = await credential_has_current_role(
                        pg_pool,
                        principal,
                        "config:read",
                    )
                except Exception:
                    logger.exception(
                        "SSE credential revalidation failed for credential %s",
                        principal.credential_id,
                    )
                    terminal_reason = "credential_authority_unavailable"
                    yield stream_close_event(terminal_reason)
                    return
                if not authorized:
                    terminal_reason = "credential_revoked"
                    yield stream_close_event(terminal_reason)
                    return
                authority_timer = asyncio.create_task(
                    asyncio.sleep(credential_check_interval_seconds)
                )

            if update_task in done:
                update = update_task.result()
                if update is None:
                    yield stream_close_event(
                        subscription.close_reason or "client_disconnect"
                    )
                    return
                update_task = asyncio.create_task(_next_update(subscription))
                if update.project_version <= snapshot_version:
                    continue
                yield update.event
    except asyncio.CancelledError:
        raise
    finally:
        pending = [
            task
            for task in (update_task, authority_timer)
            if task is not None and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await broadcaster.remove_connection(
            subscription,
            reason=terminal_reason or "client_disconnect",
        )
        logger.debug(
            "SSE connection %s disconnected for project %s",
            subscription.connection_id,
            subscription.project_id,
        )


async def _next_update(subscription: SSESubscription) -> ProjectEvent | None:
    """Prefer an explicit close signal over any queued stale backlog."""
    if subscription.close_event.is_set():
        return None
    queue_task = asyncio.create_task(subscription.queue.get())
    close_task = asyncio.create_task(subscription.close_event.wait())
    try:
        await asyncio.wait(
            {queue_task, close_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if subscription.close_event.is_set():
            return None
        return queue_task.result()
    finally:
        for task in (queue_task, close_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(queue_task, close_task, return_exceptions=True)
