"""Database-backed human login and opaque admin sessions."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.config import Settings
from app.models import (
    LoginRequest,
    ProjectAccess,
    RegistrationRequest,
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


@router.post("/login", response_model=UserIdentity)
async def login(
    body: LoginRequest, request: Request, response: Response
) -> UserIdentity:
    settings: Settings = request.app.state.settings
    require_allowed_origin(request, settings)
    email = str(body.email).strip().lower()
    now = datetime.now(timezone.utc)

    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                """
                SELECT user_id, email, password_hash, active,
                       failed_login_attempts, locked_until
                FROM admin_users
                WHERE email = $1
                FOR UPDATE
                """,
                email,
            )
            password_hash = (
                str(user["password_hash"]) if user is not None else DUMMY_PASSWORD_HASH
            )
            password_valid = verify_password(password_hash, body.password)
            locked = bool(
                user is not None
                and user["locked_until"] is not None
                and user["locked_until"] > now
            )
            valid = bool(
                user is not None and user["active"] and not locked and password_valid
            )
            if not valid:
                if user is not None and user["active"] and not locked:
                    failures = int(user["failed_login_attempts"]) + 1
                    lock_until = (
                        now + timedelta(seconds=settings.login_lock_seconds)
                        if failures >= settings.login_failure_limit
                        else None
                    )
                    await conn.execute(
                        """
                        UPDATE admin_users
                        SET failed_login_attempts = $2,
                            locked_until = $3,
                            updated_at = NOW()
                        WHERE user_id = $1
                        """,
                        user["user_id"],
                        failures,
                        lock_until,
                    )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid email or password",
                )

            user_id = str(user["user_id"])
            projects = await _project_access(conn, user_id)

            await conn.execute(
                """
                UPDATE admin_users
                SET failed_login_attempts = 0, locked_until = NULL, updated_at = NOW()
                WHERE user_id = $1
                """,
                user["user_id"],
            )
            session_token, csrf_token = await _start_session(
                conn, user["user_id"], settings, now
            )

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
) -> UserIdentity:
    settings: Settings = request.app.state.settings
    require_allowed_origin(request, settings)
    email = str(body.email).strip().lower()
    now = datetime.now(timezone.utc)

    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            user_id = await conn.fetchval(
                """
                INSERT INTO admin_users (user_id, email, password_hash)
                VALUES ($1, $2, $3)
                ON CONFLICT (email) DO NOTHING
                RETURNING user_id
                """,
                uuid.uuid4(),
                email,
                hash_password(body.password),
            )
            if user_id is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="An account already exists for this email",
                )
            session_token, csrf_token = await _start_session(
                conn, user_id, settings, now
            )

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
