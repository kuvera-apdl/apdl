"""Database-backed human login and opaque admin sessions."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from app.config import Settings
from app.login_security import (
    GENERIC_LOGIN_ERROR,
    THROTTLED_LOGIN_ERROR,
    LoginSource,
    build_login_source,
    clear_login_source_risk,
    preflight_auth_rate_limit,
    preflight_login,
    record_failed_login,
    set_device_cookie,
)
from app.models import (
    AuthCapabilities,
    LoginRequest,
    ProjectAccess,
    RegistrationRequest,
    SecurityNotification,
    UserIdentity,
)
from app.security import (
    CSRF_COOKIE,
    CSRF_HEADER,
    DUMMY_PASSWORD_HASH,
    SESSION_COOKIE,
    clear_session_cookies,
    hash_password,
    new_token,
    require_allowed_origin,
    set_session_cookies,
    token_hash,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["authentication"])
ACCOUNT_REGISTRATION_LOCK_ID = 4_704_656_378_673_808_212


@dataclass(frozen=True)
class AdminSession:
    session_id: str
    token_hash: str
    csrf_hash: str
    user_id: str
    email: str
    projects: dict[str, frozenset[str]]

    def identity(self) -> UserIdentity:
        return UserIdentity(
            user_id=self.user_id,
            email=self.email,
            projects=[
                ProjectAccess(project_id=project_id, roles=sorted(roles))
                for project_id, roles in sorted(self.projects.items())
            ],
        )


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def _project_access(conn, user_id: str) -> dict[str, frozenset[str]]:
    rows = await conn.fetch(
        """
        SELECT project_id, roles
        FROM admin_user_projects
        WHERE user_id = $1
        ORDER BY project_id
        """,
        uuid.UUID(user_id),
    )
    return {
        str(row["project_id"]): frozenset(str(role) for role in row["roles"])
        for row in rows
    }


async def _start_session(
    conn,
    user_id: uuid.UUID,
    settings: Settings,
    now: datetime,
) -> tuple[str, str]:
    await conn.execute(
        """
        DELETE FROM admin_sessions
        WHERE expires_at <= NOW()
           OR last_seen_at <= NOW() - ($1 * INTERVAL '1 second')
           OR revoked_at IS NOT NULL
        """,
        settings.session_idle_seconds,
    )
    session_token = new_token()
    csrf_token = new_token()
    await conn.execute(
        """
        INSERT INTO admin_sessions (
            session_id, user_id, token_hash, csrf_hash, expires_at
        ) VALUES ($1, $2, $3, $4, $5)
        """,
        uuid.uuid4(),
        user_id,
        token_hash(session_token),
        token_hash(csrf_token),
        now + timedelta(seconds=settings.session_ttl_seconds),
    )
    return session_token, csrf_token


async def require_session(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> AdminSession:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required"
        )

    digest = token_hash(token)
    async with request.app.state.pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT s.session_id, s.token_hash, s.csrf_hash, s.user_id, u.email
            FROM admin_sessions AS s
            JOIN admin_users AS u ON u.user_id = s.user_id
            WHERE s.token_hash = $1
              AND s.revoked_at IS NULL
              AND s.expires_at > NOW()
              AND s.last_seen_at > NOW() - ($2 * INTERVAL '1 second')
              AND u.active
            """,
            digest,
            settings.session_idle_seconds,
        )
        if row is None or not secrets.compare_digest(digest, str(row["token_hash"])):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired"
            )
        await conn.execute(
            "UPDATE admin_sessions SET last_seen_at = NOW() WHERE session_id = $1",
            row["session_id"],
        )
        projects = await _project_access(conn, str(row["user_id"]))

    return AdminSession(
        session_id=str(row["session_id"]),
        token_hash=str(row["token_hash"]),
        csrf_hash=str(row["csrf_hash"]),
        user_id=str(row["user_id"]),
        email=str(row["email"]),
        projects=projects,
    )


def require_csrf(request: Request, session: AdminSession) -> None:
    header_token = request.headers.get(CSRF_HEADER, "")
    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    if (
        not header_token
        or not cookie_token
        or not secrets.compare_digest(header_token, cookie_token)
        or not secrets.compare_digest(token_hash(header_token), session.csrf_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed"
        )


def _failed_login_response(
    source: LoginSource,
    settings: Settings,
    retry_after_seconds: int,
) -> JSONResponse:
    if retry_after_seconds > 0:
        response = JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": "auth_throttled",
                "message": THROTTLED_LOGIN_ERROR,
                "retry_after_seconds": retry_after_seconds,
            },
            headers={"Retry-After": str(retry_after_seconds)},
        )
    else:
        response = JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": GENERIC_LOGIN_ERROR},
        )
    set_device_cookie(response, source, settings)
    return response


def _registration_error(
    *,
    status_code: int,
    error: str,
    message: str,
    source: LoginSource | None = None,
    settings: Settings | None = None,
) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content={"error": error, "message": message},
    )
    if source is not None and settings is not None:
        set_device_cookie(response, source, settings)
    return response


@router.get("/capabilities", response_model=AuthCapabilities)
async def auth_capabilities(
    settings: Settings = Depends(get_settings),
) -> AuthCapabilities:
    return AuthCapabilities(registration_enabled=settings.registration_enabled)


@router.get(
    "/security-notifications",
    response_model=list[SecurityNotification],
)
async def list_security_notifications(
    request: Request,
    session: AdminSession = Depends(require_session),
) -> list[SecurityNotification]:
    async with request.app.state.pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                notification_id,
                kind,
                status,
                observed_failures,
                window_started_at,
                last_detected_at,
                created_at
            FROM admin_security_notifications
            WHERE user_id = $1
              AND status = 'unread'
            ORDER BY created_at DESC, notification_id DESC
            """,
            uuid.UUID(session.user_id),
        )
    return [SecurityNotification(**dict(row)) for row in rows]


@router.post(
    "/security-notifications/{notification_id}/acknowledge",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def acknowledge_security_notification(
    notification_id: uuid.UUID,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> Response:
    require_allowed_origin(request, request.app.state.settings)
    require_csrf(request, session)
    async with request.app.state.pg_pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE admin_security_notifications
            SET status = 'acknowledged',
                acknowledged_at = NOW()
            WHERE notification_id = $1
              AND user_id = $2
              AND status = 'unread'
            """,
            notification_id,
            uuid.UUID(session.user_id),
        )
    if result != "UPDATE 1":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Security notification not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/login", response_model=UserIdentity)
async def login(
    body: LoginRequest, request: Request, response: Response
) -> UserIdentity | Response:
    settings: Settings = request.app.state.settings
    require_allowed_origin(request, settings)
    email = str(body.email).strip().lower()
    now = datetime.now(timezone.utc)
    source = build_login_source(request, email, settings)

    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            retry_after = await preflight_login(conn, source, settings, now)
            candidate = await conn.fetchrow(
                """
                SELECT user_id, email, password_hash, active
                FROM admin_users
                WHERE email = $1
                """,
                email,
            )
    if retry_after > 0:
        return _failed_login_response(source, settings, retry_after)

    candidate_hash = (
        str(candidate["password_hash"])
        if candidate is not None
        else DUMMY_PASSWORD_HASH
    )
    password_valid = await asyncio.to_thread(
        verify_password,
        candidate_hash,
        body.password,
    )
    login_result = None
    failure_delay = 0

    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                """
                SELECT user_id, email, password_hash, active
                FROM admin_users
                WHERE email = $1
                FOR UPDATE
                """,
                email,
            )
            candidate_is_current = bool(
                candidate is not None
                and user is not None
                and secrets.compare_digest(
                    str(candidate["password_hash"]),
                    str(user["password_hash"]),
                )
            )
            valid = bool(
                user is not None
                and user["active"]
                and candidate_is_current
                and password_valid
            )
            if valid:
                user_id = str(user["user_id"])
                projects = await _project_access(conn, user_id)
                await clear_login_source_risk(conn, source)
                session_token, csrf_token = await _start_session(
                    conn,
                    user["user_id"],
                    settings,
                    now,
                )
                login_result = (user_id, projects, session_token, csrf_token)
            else:
                failure_delay = await record_failed_login(
                    conn,
                    source=source,
                    user_id=(
                        user["user_id"]
                        if user is not None and user["active"]
                        else None
                    ),
                    settings=settings,
                    now=now,
                )

    if login_result is None:
        return _failed_login_response(source, settings, failure_delay)

    user_id, projects, session_token, csrf_token = login_result
    set_device_cookie(response, source, settings)
    set_session_cookies(response, session_token, csrf_token, settings)
    return UserIdentity(
        user_id=user_id,
        email=email,
        projects=[
            ProjectAccess(project_id=project_id, roles=sorted(roles))
            for project_id, roles in sorted(projects.items())
        ],
    )


@router.post(
    "/register",
    response_model=UserIdentity,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    body: RegistrationRequest, request: Request, response: Response
) -> UserIdentity | Response:
    settings: Settings = request.app.state.settings
    require_allowed_origin(request, settings)
    if not settings.registration_enabled:
        return _registration_error(
            status_code=status.HTTP_403_FORBIDDEN,
            error="registration_disabled",
            message="Public account registration is disabled",
        )

    email = str(body.email).strip().lower()
    now = datetime.now(timezone.utc)
    source = build_login_source(request, email, settings)

    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            retry_after = await preflight_auth_rate_limit(
                conn,
                source,
                settings,
                now,
            )
            account_count = int(await conn.fetchval("SELECT count(*) FROM admin_users"))

    if retry_after > 0:
        return _failed_login_response(source, settings, retry_after)
    if account_count >= settings.max_accounts:
        return _registration_error(
            status_code=status.HTTP_409_CONFLICT,
            error="account_capacity_reached",
            message="This deployment has reached its account limit",
            source=source,
            settings=settings,
        )

    password_hash = await asyncio.to_thread(hash_password, body.password)
    capacity_reached = False
    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)",
                ACCOUNT_REGISTRATION_LOCK_ID,
            )
            locked_account_count = int(
                await conn.fetchval("SELECT count(*) FROM admin_users")
            )
            if locked_account_count >= settings.max_accounts:
                capacity_reached = True
                user_id = None
            else:
                user_id = await conn.fetchval(
                    """
                    INSERT INTO admin_users (user_id, email, password_hash)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (email) DO NOTHING
                    RETURNING user_id
                    """,
                    uuid.uuid4(),
                    email,
                    password_hash,
                )
                if user_id is None:
                    return _registration_error(
                        status_code=status.HTTP_409_CONFLICT,
                        error="account_exists",
                        message="An account already exists for this email",
                        source=source,
                        settings=settings,
                    )
                session_token, csrf_token = await _start_session(
                    conn, user_id, settings, now
                )

    if capacity_reached:
        return _registration_error(
            status_code=status.HTTP_409_CONFLICT,
            error="account_capacity_reached",
            message="This deployment has reached its account limit",
            source=source,
            settings=settings,
        )

    set_device_cookie(response, source, settings)
    set_session_cookies(response, session_token, csrf_token, settings)
    return UserIdentity(
        user_id=str(user_id),
        email=email,
        projects=[],
    )


@router.get("/me", response_model=UserIdentity)
async def me(session: AdminSession = Depends(require_session)) -> UserIdentity:
    return session.identity()


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    session: AdminSession = Depends(require_session),
) -> Response:
    settings: Settings = request.app.state.settings
    require_allowed_origin(request, settings)
    require_csrf(request, session)
    async with request.app.state.pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE admin_sessions SET revoked_at = NOW() WHERE session_id = $1",
            uuid.UUID(session.session_id),
        )
    clear_session_cookies(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
