"""Authenticated, tenant-scoped proxy from the admin UI to APDL services."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import uuid
from collections.abc import Mapping

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.responses import Response, StreamingResponse

from app.auth import AdminSession, require_csrf, require_session
from app.config import PROJECT_ID_PATTERN, SERVICE_NAMES, Settings
from app.security import require_allowed_origin

router = APIRouter(tags=["service proxy"])
logger = logging.getLogger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD"})
_JSON_MEDIA_TYPE = "application/json"
_FORWARDED_REQUEST_HEADERS = frozenset({"accept", "content-type", "if-none-match"})
_FORWARDED_RESPONSE_HEADERS = frozenset(
    {
        "cache-control",
        "content-disposition",
        "content-type",
        "etag",
        "retry-after",
        "x-cache",
    }
)
_CODEGEN_CHANGESET = re.compile(
    r"^/v1/changesets/([^/]+)(?:/(?:merge|abandon|revert|retry))?$"
)
_EPHEMERAL_CREDENTIAL_TTL_SECONDS = 300


async def _service_credential(
    request: Request,
    project_id: str,
    roles: frozenset[str],
    settings: Settings,
) -> tuple[str, str | None]:
    configured = settings.service_api_keys.get(project_id)
    if configured is not None:
        return configured, None

    raw_key = f"proj_{project_id}_{secrets.token_hex(24)}"
    credential_id = f"adminproxy-{uuid.uuid4().hex}"
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                DELETE FROM auth_credentials
                WHERE credential_id LIKE 'adminproxy-%'
                  AND expires_at <= NOW()
                """
            )
            await conn.execute(
                """
                INSERT INTO auth_credentials (
                    credential_id, project_id, key_hash, roles, expires_at
                ) VALUES ($1, $2, $3, $4, NOW() + ($5 * INTERVAL '1 second'))
                """,
                credential_id,
                project_id,
                digest,
                sorted(roles),
                _EPHEMERAL_CREDENTIAL_TTL_SECONDS,
            )
    return raw_key, credential_id


async def _remove_ephemeral_credential(
    request: Request, credential_id: str | None
) -> None:
    if credential_id is None:
        return
    try:
        async with request.app.state.pg_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM auth_credentials WHERE credential_id = $1",
                credential_id,
            )
    except Exception:
        logger.exception("Failed to remove ephemeral credential %s", credential_id)


async def _start_mutation_audit(
    request: Request,
    session: AdminSession,
    project_id: str,
    role: str,
    service: str,
    path: str,
) -> uuid.UUID | None:
    if request.method in _SAFE_METHODS:
        return None
    audit_id = uuid.uuid4()
    async with request.app.state.pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admin_proxy_audit (
                audit_id, user_id, actor_email, project_id,
                required_role, service, method, path
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            audit_id,
            uuid.UUID(session.user_id),
            session.email,
            project_id,
            role,
            service,
            request.method,
            path,
        )
    return audit_id


async def _finish_mutation_audit(
    request: Request, audit_id: uuid.UUID | None, status_code: int
) -> None:
    if audit_id is None:
        return
    try:
        async with request.app.state.pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE admin_proxy_audit
                SET status_code = $2, completed_at = NOW()
                WHERE audit_id = $1
                """,
                audit_id,
                status_code,
            )
    except Exception:
        # The immutable attempt row already exists. Do not make a completed
        # upstream mutation look retryable merely because its status update
        # could not be written.
        logger.exception("Failed to complete admin proxy audit %s", audit_id)


async def _session_is_active(
    request: Request, session: AdminSession, settings: Settings
) -> bool:
    async with request.app.state.pg_pool.acquire() as conn:
        return bool(
            await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM admin_sessions AS s
                    JOIN admin_users AS u ON u.user_id = s.user_id
                    WHERE s.session_id = $1
                      AND s.revoked_at IS NULL
                      AND s.expires_at > NOW()
                      AND s.last_seen_at > NOW() - ($2 * INTERVAL '1 second')
                      AND u.active
                )
                """,
                uuid.UUID(session.session_id),
                settings.session_idle_seconds,
            )
        )


async def _authorized_sse(
    response, request, session, settings, credential_id: str | None
):
    try:
        async for chunk in response.aiter_raw():
            if not await _session_is_active(request, session, settings):
                yield b"event: auth_expired\ndata: {}\n\n"
                return
            yield chunk
    finally:
        await response.aclose()
        await _remove_ephemeral_credential(request, credential_id)


def required_role(service: str, method: str, path: str) -> str | None:
    if method == "GET" and path in {"/health", "/ready"}:
        return None
    if service == "ingestion":
        return "events:write" if method == "POST" and path == "/v1/events" else ""
    if service == "config":
        if method == "GET" and path in {"/v1/flags", "/v1/stream"}:
            return "config:read"
        if method == "POST" and path == "/v1/evaluate":
            return "config:evaluate"
        if path == "/v1/admin" or path.startswith("/v1/admin/"):
            return "config:write"
        return ""
    if service == "query":
        return "query:read" if path.startswith("/v1/query/") else ""
    if service == "agents":
        if not path.startswith("/v1/agents"):
            return ""
        if method == "GET":
            return "agents:read"
        if method == "POST" and path == "/v1/agents/trigger":
            return "agents:run"
        if method == "POST" and path.endswith("/approve"):
            return "agents:approve"
        if (
            method == "POST" and path in {"/v1/agents/custom", "/v1/agents/custom/test"}
        ) or (method in {"PUT", "DELETE"} and path.startswith("/v1/agents/custom/")):
            return "agents:manage"
        return ""
    if service == "codegen":
        if method == "GET" and (
            path.startswith("/v1/changesets") or path.startswith("/v1/connections/")
        ):
            return "agents:read"
        if method == "POST" and path.endswith("/merge"):
            return "agents:approve"
        if (method == "POST" and path == "/v1/changesets") or (
            method == "POST" and re.search(r"/(?:abandon|revert|retry)$", path)
        ):
            return "agents:manage"
        return ""
    return ""


def _assert_tenant_value(value: object, project_id: str) -> None:
    if value is not None and str(value) != project_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Project mismatch"
        )


async def _request_body(
    request: Request,
    settings: Settings,
    project_id: str,
    *,
    require_json: bool = False,
) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > settings.max_request_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid content length"
            ) from exc
    body = await request.body()
    if len(body) > settings.max_request_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    media_type = (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if body and require_json and media_type != _JSON_MEDIA_TYPE:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Codegen request bodies must use application/json",
        )
    if body and media_type == _JSON_MEDIA_TYPE:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return body
        if isinstance(payload, Mapping):
            _assert_tenant_value(payload.get("project_id"), project_id)
    return body


async def _require_codegen_scope(
    request: Request,
    project_id: str,
    path: str,
    settings: Settings,
) -> None:
    connection_prefix = "/v1/connections/"
    if path.startswith(connection_prefix):
        _assert_tenant_value(
            path[len(connection_prefix) :].split("/", 1)[0], project_id
        )
        return
    match = _CODEGEN_CHANGESET.fullmatch(path)
    if match is None:
        return
    token = settings.internal_token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Codegen is not configured",
        )
    response = await request.app.state.http_client.get(
        f"{settings.service_urls['codegen'].rstrip('/')}/v1/changesets/{match.group(1)}",
        headers={"X-APDL-Internal-Token": token},
    )
    if response.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Changeset not found"
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to authorize changeset",
        )
    try:
        changeset_project = response.json()["project_id"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Invalid codegen response"
        ) from exc
    if changeset_project != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Changeset not found"
        )


@router.api_route(
    "/api/projects/{project_id}/{service}/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "DELETE"],
)
async def proxy_service(
    project_id: str,
    service: str,
    path: str,
    request: Request,
    session: AdminSession = Depends(require_session),
):
    settings: Settings = request.app.state.settings
    if PROJECT_ID_PATTERN.fullmatch(project_id) is None or service not in SERVICE_NAMES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    roles = session.projects.get(project_id)
    if roles is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    upstream_path = f"/{path}"
    role = required_role(service, request.method, upstream_path)
    if role == "":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Route not available"
        )
    if role is not None and role not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role"
        )

    if request.method not in _SAFE_METHODS:
        require_allowed_origin(request, settings)
        require_csrf(request, session)

    if "api_key" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Credentials are not accepted from the browser",
        )
    for value in request.query_params.getlist("project_id"):
        _assert_tenant_value(value, project_id)
    body = await _request_body(
        request,
        settings,
        project_id,
        require_json=service == "codegen",
    )
    if service == "codegen":
        await _require_codegen_scope(request, project_id, upstream_path, settings)

    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() in _FORWARDED_REQUEST_HEADERS
    }
    ephemeral_credential_id: str | None = None
    if service == "codegen":
        if not settings.internal_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Codegen is not configured",
            )
        headers["X-APDL-Internal-Token"] = settings.internal_token
    else:
        api_key, ephemeral_credential_id = await _service_credential(
            request, project_id, roles, settings
        )
        headers["X-API-Key"] = api_key

    upstream_url = f"{settings.service_urls[service].rstrip('/')}{upstream_path}"
    upstream_request = request.app.state.http_client.build_request(
        request.method,
        upstream_url,
        params=request.query_params.multi_items(),
        headers=headers,
        content=body,
    )
    try:
        audit_id = await _start_mutation_audit(
            request,
            session,
            project_id,
            role or "authenticated",
            service,
            upstream_path,
        )
    except Exception:
        await _remove_ephemeral_credential(request, ephemeral_credential_id)
        raise
    try:
        response = await request.app.state.http_client.send(
            upstream_request, stream=True
        )
    except httpx.RequestError as exc:
        await _remove_ephemeral_credential(request, ephemeral_credential_id)
        await _finish_mutation_audit(request, audit_id, status.HTTP_502_BAD_GATEWAY)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Upstream unavailable"
        ) from exc

    await _finish_mutation_audit(request, audit_id, response.status_code)

    response_headers = {
        name: value
        for name, value in response.headers.items()
        if name.lower() in _FORWARDED_RESPONSE_HEADERS
    }
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        return StreamingResponse(
            _authorized_sse(
                response,
                request,
                session,
                settings,
                ephemeral_credential_id,
            ),
            status_code=response.status_code,
            headers=response_headers,
        )
    try:
        content = await response.aread()
    finally:
        await response.aclose()
        await _remove_ephemeral_credential(request, ephemeral_credential_id)
    return Response(
        content=content, status_code=response.status_code, headers=response_headers
    )
