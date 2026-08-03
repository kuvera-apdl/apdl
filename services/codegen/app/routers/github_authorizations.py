"""Project-scoped GitHub App installation and user authorization routes."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import asyncpg
import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from app.auth import Principal
from app.github.user_authorization import (
    GitHubAuthorizationSettings,
    derive_pkce_verifier,
    discover_user_repositories,
    exchange_oauth_code,
    pkce_code_challenge,
    verify_repository_candidate,
)
from app.models.connection import Connection
from app.models.repository_authorization import (
    RepositoryAuthorization,
    RepositoryAuthorizationComplete,
    RepositoryAuthorizationStart,
    RepositoryAuthorizationStarted,
)
from app.store import repository_authorizations as store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/github", tags=["github"])
public_router = APIRouter(tags=["github"])

_AUTHORIZATION_TTL = timedelta(minutes=10)
_STATE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,512}$")
_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_ERROR_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_ASCII_DESCRIPTION_PATTERN = re.compile(r"^[\x20-\x7E]{0,512}$")
_PROJECT_ID_PATTERN = r"^[A-Za-z0-9]{1,64}$"
_MAX_GITHUB_ID = 9_223_372_036_854_775_807
_GENERIC_ERROR_REDIRECT = "/codegen?github_repository_error=authorization_failed"
_APPROVAL_REQUIRED_STATUS = "installation_approval_required"
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
}


def _state_token() -> str:
    return secrets.token_urlsafe(32)


def _state_hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303, headers=_NO_STORE_HEADERS)


def _authorization_redirect(authorization: store.ClaimedAuthorization) -> str:
    query = urlencode(
        {
            "github_repository_authorization": str(
                authorization.authorization_id
            ),
            "github_repository_project_id": authorization.project_id,
        }
    )
    return f"/codegen?{query}"


def _approval_required_redirect(authorization: store.ClaimedAuthorization) -> str:
    query = urlencode(
        {
            "github_repository_status": _APPROVAL_REQUIRED_STATUS,
            "github_repository_project_id": authorization.project_id,
        }
    )
    return f"/codegen?{query}"


async def _human_actor(
    request: Request,
    project_id: str,
) -> tuple[asyncpg.Pool, uuid.UUID]:
    principal: Principal = request.state.principal
    if not secrets.compare_digest(principal.project_id, project_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credential is not authorized for this project",
        )
    if principal.actor_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A human project actor is required",
        )
    try:
        actor_user_id = uuid.UUID(principal.actor_user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A human project actor is required",
        ) from exc
    pool: asyncpg.Pool = request.app.state.pg_pool
    if not await store.has_repository_connection_authority(
        pool,
        project_id=project_id,
        actor_user_id=actor_user_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Repository connection requires project ownership or delegated "
                "agents:manage and credentials:manage roles"
            ),
        )
    return pool, actor_user_id


def _settings_or_unavailable() -> GitHubAuthorizationSettings:
    try:
        return GitHubAuthorizationSettings.from_environment()
    except ValueError as exc:
        logger.error("GitHub user authorization is not configured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub repository authorization is unavailable",
        ) from exc


@contextlib.asynccontextmanager
async def _github_client(request: Request) -> AsyncIterator[httpx.AsyncClient]:
    configured = getattr(
        request.app.state,
        "github_authorization_http_client",
        None,
    )
    if configured is not None:
        yield configured
        return
    async with httpx.AsyncClient(timeout=20.0) as client:
        yield client


@router.post(
    "/repository-authorizations",
    response_model=RepositoryAuthorizationStarted,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def start_repository_authorization(
    body: RepositoryAuthorizationStart,
    request: Request,
) -> RepositoryAuthorizationStarted:
    """Start a user-owned GitHub App installation and OAuth proof."""
    pool, actor_user_id = await _human_actor(request, body.project_id)
    settings = _settings_or_unavailable()
    authorization_id = uuid.uuid4()
    state_token = _state_token()
    expires_at = datetime.now(timezone.utc) + _AUTHORIZATION_TTL
    await store.create_authorization(
        pool,
        authorization_id=authorization_id,
        project_id=body.project_id,
        actor_user_id=actor_user_id,
        state_hash=_state_hash(state_token),
        expires_at=expires_at,
    )
    return RepositoryAuthorizationStarted(
        authorization_id=authorization_id,
        installation_url=settings.installation_url(state_token),
        expires_at=expires_at,
    )


@router.get(
    "/repository-authorizations/{authorization_id}",
    response_model=RepositoryAuthorization,
)
async def get_repository_authorization(
    authorization_id: uuid.UUID,
    request: Request,
    project_id: str = Query(pattern=_PROJECT_ID_PATTERN),
) -> RepositoryAuthorization:
    """Return only candidates belonging to this project, actor, and flow."""
    pool, actor_user_id = await _human_actor(request, project_id)
    authorization = await store.get_authorization(
        pool,
        authorization_id=authorization_id,
        project_id=project_id,
        actor_user_id=actor_user_id,
    )
    if authorization is None:
        raise HTTPException(status_code=404, detail="Authorization not found")
    if authorization.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Authorization expired")
    return authorization


@router.post(
    "/repository-authorizations/{authorization_id}/complete",
    response_model=Connection,
)
async def complete_repository_authorization(
    authorization_id: uuid.UUID,
    body: RepositoryAuthorizationComplete,
    request: Request,
) -> Connection:
    """Activate one opaque GitHub candidate as the project's repository."""
    pool, actor_user_id = await _human_actor(request, body.project_id)
    try:
        candidate = await store.get_completion_candidate(
            pool,
            authorization_id=authorization_id,
            project_id=body.project_id,
            actor_user_id=actor_user_id,
            candidate_id=body.candidate_id,
        )
    except store.RepositoryAuthorizationNotFound as exc:
        raise HTTPException(status_code=404, detail="Authorization not found") from exc
    except store.RepositoryAuthorizationExpired as exc:
        raise HTTPException(status_code=410, detail="Authorization expired") from exc
    except store.RepositoryAuthorizationConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="Authorization cannot be completed in its current state",
        ) from exc

    settings = _settings_or_unavailable()
    try:
        async with _github_client(request) as client:
            await verify_repository_candidate(
                candidate,
                settings,
                client=client,
            )
    except httpx.HTTPStatusError as exc:
        code = 502 if exc.response.status_code >= 500 else 409
        raise HTTPException(
            status_code=code,
            detail="GitHub repository authorization could not be revalidated",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail="GitHub repository authorization could not be revalidated",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="GitHub repository authorization must be restarted",
        ) from exc

    try:
        return await store.complete_authorization(
            pool,
            authorization_id=authorization_id,
            project_id=body.project_id,
            actor_user_id=actor_user_id,
            candidate_id=body.candidate_id,
            verified_candidate=candidate,
        )
    except store.RepositoryAuthorizationNotFound as exc:
        raise HTTPException(status_code=404, detail="Authorization not found") from exc
    except store.RepositoryAuthorizationExpired as exc:
        raise HTTPException(status_code=410, detail="Authorization expired") from exc
    except store.RepositoryAuthorizationConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="Authorization cannot be completed in its current state",
        ) from exc
    except store.RepositoryAuthorizationForbidden as exc:
        raise HTTPException(
            status_code=403,
            detail="Repository connection authority was revoked",
        ) from exc


def _single_query(request: Request) -> dict[str, str] | None:
    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key in values:
            return None
        values[key] = value
    return values


def _valid_state(value: object) -> str | None:
    return value if isinstance(value, str) and _STATE_PATTERN.fullmatch(value) else None


def _setup_state(query: dict[str, str]) -> str | None:
    if set(query) != {"state", "installation_id", "setup_action"}:
        return None
    state_value = _valid_state(query.get("state"))
    if state_value is None or query.get("setup_action") not in {"install", "update"}:
        return None
    raw_installation_id = query.get("installation_id", "")
    if not raw_installation_id.isascii() or not raw_installation_id.isdecimal():
        return None
    installation_id = int(raw_installation_id)
    if installation_id < 1 or installation_id > _MAX_GITHUB_ID:
        return None
    return state_value


def _approval_request_state(query: dict[str, str]) -> str | None:
    if set(query) != {"state", "setup_action"}:
        return None
    state_value = _valid_state(query.get("state"))
    return state_value if query.get("setup_action") == "request" else None


def _oauth_code(query: dict[str, str]) -> tuple[str, str] | None:
    if set(query) != {"state", "code"}:
        return None
    state_value = _valid_state(query.get("state"))
    code = query.get("code")
    if state_value is None or code is None or _CODE_PATTERN.fullmatch(code) is None:
        return None
    return state_value, code


def _oauth_error_state(query: dict[str, str]) -> str | None:
    if not {"state", "error"}.issubset(query) or not set(query).issubset(
        {"state", "error", "error_description", "error_uri"}
    ):
        return None
    state_value = _valid_state(query.get("state"))
    error = query.get("error")
    description = query.get("error_description", "")
    error_uri = query.get("error_uri")
    if (
        state_value is None
        or error is None
        or _ERROR_PATTERN.fullmatch(error) is None
        or _ASCII_DESCRIPTION_PATTERN.fullmatch(description) is None
    ):
        return None
    if error_uri is not None:
        try:
            parsed = httpx.URL(error_uri)
        except Exception:
            return None
        if (
            parsed.scheme != "https"
            or not parsed.host
            or parsed.userinfo
            or len(error_uri) > 2048
        ):
            return None
    return state_value


@public_router.get("/github/repository-authorization/callback")
async def github_repository_authorization_callback(
    request: Request,
) -> RedirectResponse:
    """Rotate setup state or consume OAuth state and persist opaque candidates."""
    query = _single_query(request)
    if query is None:
        return _redirect(_GENERIC_ERROR_REDIRECT)
    try:
        settings = GitHubAuthorizationSettings.from_environment()
    except ValueError:
        logger.exception("GitHub user authorization callback is not configured")
        return _redirect(_GENERIC_ERROR_REDIRECT)

    pool: asyncpg.Pool = request.app.state.pg_pool
    await store.purge_expired_authorizations(pool)
    approval_state = _approval_request_state(query)
    if approval_state is not None:
        authorization = await store.cancel_installation_state(
            pool,
            state_hash=_state_hash(approval_state),
        )
        if authorization is None:
            return _redirect(_GENERIC_ERROR_REDIRECT)
        if not await store.has_repository_connection_authority(
            pool,
            project_id=authorization.project_id,
            actor_user_id=authorization.actor_user_id,
        ):
            return _redirect(_GENERIC_ERROR_REDIRECT)
        return _redirect(_approval_required_redirect(authorization))

    setup_state = _setup_state(query)
    if setup_state is not None:
        oauth_state = _state_token()
        authorization = await store.rotate_installation_state(
            pool,
            state_hash=_state_hash(setup_state),
            oauth_state_hash=_state_hash(oauth_state),
        )
        if authorization is None:
            return _redirect(_GENERIC_ERROR_REDIRECT)
        if not await store.has_repository_connection_authority(
            pool,
            project_id=authorization.project_id,
            actor_user_id=authorization.actor_user_id,
        ):
            return _redirect(_GENERIC_ERROR_REDIRECT)
        verifier = derive_pkce_verifier(oauth_state, settings.client_secret)
        return _redirect(
            settings.oauth_url(oauth_state, pkce_code_challenge(verifier))
        )

    oauth = _oauth_code(query)
    error_state = _oauth_error_state(query)
    callback_state = oauth[0] if oauth is not None else error_state
    if callback_state is None:
        return _redirect(_GENERIC_ERROR_REDIRECT)

    authorization = await store.consume_oauth_state(
        pool,
        state_hash=_state_hash(callback_state),
        consumed_state_hash=_state_hash(_state_token()),
    )
    if authorization is None:
        return _redirect(_GENERIC_ERROR_REDIRECT)
    if error_state is not None:
        return _redirect(_GENERIC_ERROR_REDIRECT)
    if not await store.has_repository_connection_authority(
        pool,
        project_id=authorization.project_id,
        actor_user_id=authorization.actor_user_id,
    ):
        return _redirect(_GENERIC_ERROR_REDIRECT)

    try:
        async with _github_client(request) as client:
            verifier = derive_pkce_verifier(
                callback_state,
                settings.client_secret,
            )
            token = await exchange_oauth_code(
                oauth[1],
                verifier,
                settings,
                client=client,
            )
            user, repositories = await discover_user_repositories(
                token,
                settings,
                client=client,
            )
        await store.save_discovered_repositories(
            pool,
            authorization=authorization,
            github_user_id=user.id,
            github_login=user.login,
            repositories=repositories,
        )
    except (httpx.HTTPError, ValueError, store.RepositoryAuthorizationError):
        logger.exception("GitHub repository authorization callback failed")
        return _redirect(_GENERIC_ERROR_REDIRECT)

    return _redirect(_authorization_redirect(authorization))
