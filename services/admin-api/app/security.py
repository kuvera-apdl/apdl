"""Password, opaque-token, origin, and cookie security primitives."""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, Response, status

from app.config import Settings

SESSION_COOKIE = "apdl_admin_session"
CSRF_COOKIE = "apdl_admin_csrf"
CSRF_HEADER = "x-csrf-token"

password_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)
DUMMY_PASSWORD_HASH = password_hasher.hash("not-a-real-password")


def hash_password(password: str) -> str:
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Password must contain between 12 and 1024 characters")
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def require_allowed_origin(request: Request, settings: Settings) -> None:
    origin = request.headers.get("origin", "").rstrip("/")
    if origin not in settings.allowed_origins:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Origin not allowed"
        )


def set_session_cookies(
    response: Response,
    session_token: str,
    csrf_token: str,
    settings: Settings,
) -> None:
    common = {
        "secure": settings.cookie_secure,
        "samesite": "strict",
        "max_age": settings.session_ttl_seconds,
    }
    response.set_cookie(
        SESSION_COOKIE, session_token, httponly=True, path="/api", **common
    )
    # The SPA must read the double-submit value from document.cookie while the
    # actual session remains HttpOnly and restricted to /api.
    response.set_cookie(CSRF_COOKIE, csrf_token, httponly=False, path="/", **common)


def clear_session_cookies(response: Response, settings: Settings) -> None:
    common = {
        "secure": settings.cookie_secure,
        "samesite": "strict",
    }
    response.delete_cookie(SESSION_COOKIE, httponly=True, path="/api", **common)
    response.delete_cookie(CSRF_COOKIE, httponly=False, path="/", **common)
