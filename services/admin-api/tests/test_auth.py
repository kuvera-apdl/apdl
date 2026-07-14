from __future__ import annotations

import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.security import hash_password
from conftest import make_settings


class FakeConnection:
    def __init__(self) -> None:
        self.user_id = uuid.UUID("20000000-0000-4000-8000-000000000002")
        self.password_hash = hash_password("a-correct-horse-battery-staple")
        self.failed_login_attempts = 0
        self.locked_until: datetime | None = None
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    @asynccontextmanager
    async def transaction(self):
        snapshot = (self.failed_login_attempts, self.locked_until)
        try:
            yield
        except Exception:
            self.failed_login_attempts, self.locked_until = snapshot
            raise

    async def fetchrow(self, query: str, *args):
        if "FROM admin_users" in query:
            if args[0] != "admin@example.com":
                return None
            return {
                "user_id": self.user_id,
                "email": "admin@example.com",
                "password_hash": self.password_hash,
                "active": True,
                "failed_login_attempts": self.failed_login_attempts,
                "locked_until": self.locked_until,
            }
        raise AssertionError(f"Unexpected fetchrow: {query}")

    async def fetch(self, query: str, *args):
        assert "FROM admin_user_projects" in query
        assert args == (self.user_id,)
        return [{"project_id": "demo", "roles": ["config:read", "config:write"]}]

    async def execute(self, query: str, *args):
        self.executions.append((query, args))
        if "SET failed_login_attempts = $2" in query:
            self.failed_login_attempts = int(args[1])
            self.locked_until = args[2]
        elif "SET failed_login_attempts = 0" in query:
            self.failed_login_attempts = 0
            self.locked_until = None
        return "OK"


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def make_client(connection: FakeConnection) -> TestClient:
    app = FastAPI()
    app.state.settings = make_settings()
    app.state.pg_pool = FakePool(connection)
    app.include_router(auth.router)
    return TestClient(app)


def test_login_sets_opaque_httponly_session_and_returns_no_service_secret() -> None:
    connection = FakeConnection()
    with make_client(connection) as client:
        response = client.post(
            "/api/auth/login",
            headers={"Origin": "http://admin.test"},
            json={
                "email": "admin@example.com",
                "password": "a-correct-horse-battery-staple",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(connection.user_id),
        "email": "admin@example.com",
        "projects": [{"project_id": "demo", "roles": ["config:read", "config:write"]}],
    }
    assert "api_key" not in response.text.lower()
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(
        value for value in cookies if value.startswith("apdl_admin_session=")
    )
    csrf_cookie = next(
        value for value in cookies if value.startswith("apdl_admin_csrf=")
    )
    assert "HttpOnly" in session_cookie
    assert "SameSite=strict" in session_cookie
    assert "Path=/api" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "Path=/" in csrf_cookie

    insert = next(
        item
        for item in connection.executions
        if "INSERT INTO admin_sessions" in item[0]
    )
    stored_token_hash = insert[1][2]
    raw_session = session_cookie.split(";", 1)[0].split("=", 1)[1]
    assert stored_token_hash == hashlib.sha256(raw_session.encode()).hexdigest()
    assert raw_session != stored_token_hash


def test_login_uses_a_generic_error_for_unknown_users() -> None:
    connection = FakeConnection()
    with make_client(connection) as client:
        response = client.post(
            "/api/auth/login",
            headers={"Origin": "http://admin.test"},
            json={"email": "missing@example.com", "password": "wrong-password"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid email or password"}


def test_failed_login_attempts_commit_and_lock_the_account() -> None:
    connection = FakeConnection()
    with make_client(connection) as client:
        for expected_failures in range(1, 6):
            response = client.post(
                "/api/auth/login",
                headers={"Origin": "http://admin.test"},
                json={
                    "email": "admin@example.com",
                    "password": "wrong-password",
                },
            )

            assert response.status_code == 401
            assert response.json() == {"detail": "Invalid email or password"}
            assert connection.failed_login_attempts == expected_failures
            if expected_failures < 5:
                assert connection.locked_until is None

        assert connection.locked_until is not None
        assert connection.locked_until > datetime.now(timezone.utc)

        locked_response = client.post(
            "/api/auth/login",
            headers={"Origin": "http://admin.test"},
            json={
                "email": "admin@example.com",
                "password": "a-correct-horse-battery-staple",
            },
        )

    assert locked_response.status_code == 401
    assert connection.failed_login_attempts == 5
    assert not any(
        "INSERT INTO admin_sessions" in query for query, _ in connection.executions
    )


def test_successful_login_after_lock_expiry_resets_failure_state() -> None:
    connection = FakeConnection()
    connection.failed_login_attempts = 5
    connection.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)

    with make_client(connection) as client:
        response = client.post(
            "/api/auth/login",
            headers={"Origin": "http://admin.test"},
            json={
                "email": "admin@example.com",
                "password": "a-correct-horse-battery-staple",
            },
        )

    assert response.status_code == 200
    assert connection.failed_login_attempts == 0
    assert connection.locked_until is None


def test_login_rejects_cross_site_origins_before_checking_credentials() -> None:
    connection = FakeConnection()
    with make_client(connection) as client:
        response = client.post(
            "/api/auth/login",
            headers={"Origin": "https://attacker.example"},
            json={"email": "admin@example.com", "password": "anything"},
        )

    assert response.status_code == 403
    assert connection.executions == []
