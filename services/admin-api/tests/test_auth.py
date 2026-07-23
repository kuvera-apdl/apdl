from __future__ import annotations

import hashlib
import uuid
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.security import hash_password, token_hash
from conftest import make_settings


class FakeConnection:
    def __init__(self) -> None:
        self.user_id = uuid.UUID("20000000-0000-4000-8000-000000000002")
        self.password_hash = hash_password("a-correct-horse-battery-staple")
        self.active = True
        self.rate_buckets: dict[tuple[str, str], tuple[datetime, int]] = {}
        self.source_risks: dict[tuple[str, str, str], tuple[int, datetime]] = {}
        self.account_failures = 0
        self.account_window_started_at: datetime | None = None
        self.notifications: list[dict[str, object]] = []
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    @asynccontextmanager
    async def transaction(self):
        snapshot = deepcopy(
            (
                self.rate_buckets,
                self.source_risks,
                self.account_failures,
                self.account_window_started_at,
                self.notifications,
            )
        )
        try:
            yield
        except Exception:
            (
                self.rate_buckets,
                self.source_risks,
                self.account_failures,
                self.account_window_started_at,
                self.notifications,
            ) = snapshot
            raise

    async def fetchrow(self, query: str, *args):
        if "INSERT INTO admin_login_rate_buckets" in query:
            scope, key_hash, window_seconds, now = args
            key = (str(scope), str(key_hash))
            previous = self.rate_buckets.get(key)
            if (
                previous is None
                or previous[0] <= now - timedelta(seconds=int(window_seconds))
            ):
                value = (now, 1)
            else:
                value = (previous[0], previous[1] + 1)
            self.rate_buckets[key] = value
            return {
                "window_started_at": value[0],
                "attempt_count": value[1],
            }
        if "INSERT INTO admin_login_account_risk" in query:
            _, _, now, window_seconds = args
            if (
                self.account_window_started_at is None
                or self.account_window_started_at
                <= now - timedelta(seconds=int(window_seconds))
            ):
                self.account_window_started_at = now
                self.account_failures = 1
            else:
                self.account_failures += 1
            return {
                "window_started_at": self.account_window_started_at,
                "failure_count": self.account_failures,
            }
        if "FROM admin_users" in query:
            if args[0] != "admin@example.com":
                return None
            return {
                "user_id": self.user_id,
                "email": "admin@example.com",
                "password_hash": self.password_hash,
                "active": self.active,
            }
        raise AssertionError(f"Unexpected fetchrow: {query}")

    async def fetchval(self, query: str, *args):
        if "SELECT next_allowed_at" in query:
            risk = self.source_risks.get(
                (str(args[0]), str(args[1]), str(args[2]))
            )
            return risk[1] if risk is not None else None
        if "INSERT INTO admin_login_source_risk" in query:
            key = (str(args[0]), str(args[1]), str(args[2]))
            now = args[3]
            previous = self.source_risks.get(key)
            failures = 1 if previous is None else previous[0] + 1
            next_allowed_at = now if previous is None else previous[1]
            self.source_risks[key] = (failures, next_allowed_at)
            return failures
        raise AssertionError(f"Unexpected fetchval: {query}")

    async def fetch(self, query: str, *args):
        assert "FROM admin_user_projects" in query
        assert args == (self.user_id,)
        return [{"project_id": "demo", "roles": ["config:read", "config:write"]}]

    async def execute(self, query: str, *args):
        self.executions.append((query, args))
        if (
            "DELETE FROM admin_login_source_risk" in query
            and "WHERE email_hash = $1" in query
        ):
            email_hash, network_hash, device_hash = (str(arg) for arg in args)
            self.source_risks.pop(("network", network_hash, email_hash), None)
            self.source_risks.pop(("device", device_hash, email_hash), None)
        elif "UPDATE admin_login_source_risk" in query:
            key = (str(args[0]), str(args[1]), str(args[2]))
            failures, next_allowed_at = self.source_risks[key]
            proposed = args[3] + timedelta(seconds=int(args[4]))
            self.source_risks[key] = (failures, max(next_allowed_at, proposed))
        elif "UPDATE admin_security_notifications" in query:
            unread = next(
                (
                    notification
                    for notification in self.notifications
                    if notification["status"] == "unread"
                ),
                None,
            )
            if unread is None:
                return "UPDATE 0"
            unread["observed_failures"] = max(
                int(unread["observed_failures"]),
                int(args[1]),
            )
            return "UPDATE 1"
        elif "INSERT INTO admin_security_notifications" in query:
            self.notifications.append(
                {
                    "notification_id": args[0],
                    "status": "unread",
                    "observed_failures": args[2],
                }
            )
        return "OK"


class FakePool:
    def __init__(self, connection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        if hasattr(self.connection, "pool_acquisitions"):
            self.connection.pool_acquisitions += 1
            self.connection.pool_depth += 1
        try:
            yield self.connection
        finally:
            if hasattr(self.connection, "pool_depth"):
                self.connection.pool_depth -= 1


def make_client(connection, *, settings=None) -> TestClient:
    app = FastAPI()
    app.state.settings = settings or make_settings()
    app.state.pg_pool = FakePool(connection)
    app.include_router(auth.router)
    return TestClient(app, client=("127.0.0.1", 50000))


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
    device_cookie = next(
        value for value in cookies if value.startswith("apdl_admin_device=")
    )
    assert "HttpOnly" in device_cookie
    assert "SameSite=strict" in device_cookie
    assert "Path=/api/auth" in device_cookie

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


def test_failed_logins_receive_a_source_scoped_progressive_delay() -> None:
    connection = FakeConnection()
    with make_client(connection) as client:
        for expected_failures in range(1, 4):
            response = client.post(
                "/api/auth/login",
                headers={"Origin": "http://admin.test"},
                json={
                    "email": "admin@example.com",
                    "password": "wrong-password",
                },
            )

            if expected_failures < 3:
                assert response.status_code == 401
                assert response.json() == {"detail": "Invalid email or password"}
            else:
                assert response.status_code == 429
                assert response.json() == {
                    "error": "auth_throttled",
                    "message": "Too many attempts. Try again later.",
                    "retry_after_seconds": 1,
                }
                assert response.headers["retry-after"] == "1"

        throttled = client.post(
            "/api/auth/login",
            headers={"Origin": "http://admin.test"},
            json={
                "email": "admin@example.com",
                "password": "a-correct-horse-battery-staple",
            },
        )

    assert throttled.status_code == 429
    assert not any(
        "INSERT INTO admin_sessions" in query for query, _ in connection.executions
    )


def test_account_risk_never_blocks_a_correct_password_from_a_clean_source() -> None:
    connection = FakeConnection()
    connection.account_failures = 100
    connection.account_window_started_at = datetime.now(timezone.utc)

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
    assert any(
        "INSERT INTO admin_sessions" in query for query, _ in connection.executions
    )


def test_rotating_a_device_does_not_bypass_the_network_progressive_delay() -> None:
    connection = FakeConnection()
    with make_client(connection) as attacker:
        for _ in range(3):
            delayed = attacker.post(
                "/api/auth/login",
                headers={
                    "Origin": "http://admin.test",
                    "X-Forwarded-For": "203.0.113.10",
                },
                json={"email": "admin@example.com", "password": "wrong-password"},
            )
    assert delayed.status_code == 429

    with make_client(connection) as clean_device:
        response = clean_device.post(
            "/api/auth/login",
            headers={
                "Origin": "http://admin.test",
                "X-Forwarded-For": "203.0.113.10",
            },
            json={"email": "admin@example.com", "password": "wrong-password"},
        )

    assert response.status_code == 429


def test_correct_password_recovers_from_a_different_network_after_an_attack() -> None:
    connection = FakeConnection()
    with make_client(connection) as attacker:
        for _ in range(3):
            attacker.post(
                "/api/auth/login",
                headers={
                    "Origin": "http://admin.test",
                    "X-Forwarded-For": "203.0.113.10",
                },
                json={"email": "admin@example.com", "password": "wrong-password"},
            )

    with make_client(connection) as account_owner:
        response = account_owner.post(
            "/api/auth/login",
            headers={
                "Origin": "http://admin.test",
                "X-Forwarded-For": "198.51.100.20",
            },
            json={
                "email": "admin@example.com",
                "password": "a-correct-horse-battery-staple",
            },
        )

    assert response.status_code == 200


def test_account_threshold_creates_one_durable_security_notification() -> None:
    connection = FakeConnection()
    settings = make_settings(login_account_notice_threshold=3)
    with make_client(connection, settings=settings) as client:
        for _ in range(3):
            client.post(
                "/api/auth/login",
                headers={"Origin": "http://admin.test"},
                json={"email": "admin@example.com", "password": "wrong-password"},
            )

    assert connection.account_failures == 3
    assert len(connection.notifications) == 1
    assert connection.notifications[0]["observed_failures"] == 3


class SecurityNotificationConnection:
    def __init__(self) -> None:
        self.notification_id = uuid.UUID("70000000-0000-4000-8000-000000000007")
        self.user_id = uuid.UUID("20000000-0000-4000-8000-000000000002")
        self.acknowledged = False
        self.now = datetime.now(timezone.utc)

    async def fetch(self, query: str, *args):
        assert "FROM admin_security_notifications" in query
        assert args == (self.user_id,)
        if self.acknowledged:
            return []
        return [
            {
                "notification_id": self.notification_id,
                "kind": "suspicious_login_activity",
                "status": "unread",
                "observed_failures": 50,
                "window_started_at": self.now - timedelta(hours=1),
                "last_detected_at": self.now,
                "created_at": self.now,
            }
        ]

    async def execute(self, query: str, *args):
        assert "UPDATE admin_security_notifications" in query
        if args != (self.notification_id, self.user_id) or self.acknowledged:
            return "UPDATE 0"
        self.acknowledged = True
        return "UPDATE 1"


def make_security_notification_client(
    connection: SecurityNotificationConnection,
) -> tuple[TestClient, str]:
    csrf = "notification-csrf-token"
    session = auth.AdminSession(
        session_id="10000000-0000-4000-8000-000000000001",
        token_hash="a" * 64,
        csrf_hash=token_hash(csrf),
        user_id=str(connection.user_id),
        email="admin@example.com",
        projects={},
    )
    app = FastAPI()
    app.state.settings = make_settings()
    app.state.pg_pool = FakePool(connection)
    app.include_router(auth.router)
    app.dependency_overrides[auth.require_session] = lambda: session
    return TestClient(app), csrf


def test_security_notification_can_be_read_and_acknowledged() -> None:
    connection = SecurityNotificationConnection()
    client, csrf = make_security_notification_client(connection)
    with client:
        listed = client.get("/api/auth/security-notifications")
        client.cookies.set("apdl_admin_csrf", csrf, path="/")
        acknowledged = client.post(
            f"/api/auth/security-notifications/{connection.notification_id}/acknowledge",
            headers={
                "Origin": "http://admin.test",
                "X-CSRF-Token": csrf,
            },
        )
        empty = client.get("/api/auth/security-notifications")

    assert listed.status_code == 200
    assert listed.json()[0]["kind"] == "suspicious_login_activity"
    assert listed.json()[0]["observed_failures"] == 50
    assert acknowledged.status_code == 204
    assert empty.json() == []


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


class RegistrationConnection:
    def __init__(
        self,
        *,
        account_exists: bool = False,
        account_count: int = 0,
        locked_account_count: int | None = None,
    ) -> None:
        self.user_id = uuid.UUID("30000000-0000-4000-8000-000000000003")
        self.account_exists = account_exists
        self.account_count = account_count
        self.locked_account_count = locked_account_count
        self.count_reads = 0
        self.rate_buckets: dict[tuple[str, str], tuple[datetime, int]] = {}
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.insert_attempts = 0
        self.pool_acquisitions = 0
        self.pool_depth = 0

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, query: str, *args):
        assert "INSERT INTO admin_login_rate_buckets" in query
        scope, key_hash, window_seconds, now = args
        key = (str(scope), str(key_hash))
        previous = self.rate_buckets.get(key)
        if (
            previous is None
            or previous[0] <= now - timedelta(seconds=int(window_seconds))
        ):
            value = (now, 1)
        else:
            value = (previous[0], previous[1] + 1)
        self.rate_buckets[key] = value
        return {"window_started_at": value[0], "attempt_count": value[1]}

    async def fetchval(self, query: str, *args):
        self.executions.append((query, args))
        if "SELECT count(*) FROM admin_users" in query:
            self.count_reads += 1
            if self.count_reads >= 2 and self.locked_account_count is not None:
                return self.locked_account_count
            return self.account_count
        assert "INSERT INTO admin_users" in query
        self.insert_attempts += 1
        assert args[1] == "new-admin@example.com"
        assert str(args[2]).startswith("$argon2id$")
        if self.account_exists:
            return None
        self.account_count += 1
        return self.user_id

    async def execute(self, query: str, *args):
        self.executions.append((query, args))
        return "OK"


def test_registration_creates_zero_project_user_and_starts_session() -> None:
    connection = RegistrationConnection()
    with make_client(connection) as client:
        response = client.post(
            "/api/auth/register",
            headers={"Origin": "http://admin.test"},
            json={
                "email": "NEW-ADMIN@example.com",
                "password": "a-new-correct-horse-password",
            },
        )

    assert response.status_code == 201
    assert response.json() == {
        "user_id": str(connection.user_id),
        "email": "new-admin@example.com",
        "projects": [],
    }
    assert connection.insert_attempts == 1
    assert not any("admin_user_projects" in query for query, _ in connection.executions)
    assert any(
        "INSERT INTO admin_sessions" in query for query, _ in connection.executions
    )
    assert any(
        cookie.startswith("apdl_admin_session=") and "HttpOnly" in cookie
        for cookie in response.headers.get_list("set-cookie")
    )
    assert any(
        cookie.startswith("apdl_admin_device=") and "Path=/api/auth" in cookie
        for cookie in response.headers.get_list("set-cookie")
    )


def test_registration_rejects_existing_email_without_starting_session() -> None:
    connection = RegistrationConnection(account_exists=True)
    with make_client(connection) as client:
        response = client.post(
            "/api/auth/register",
            headers={"Origin": "http://admin.test"},
            json={
                "email": "new-admin@example.com",
                "password": "a-new-correct-horse-password",
            },
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": "account_exists",
        "message": "An account already exists for this email",
    }
    assert not any(
        "INSERT INTO admin_sessions" in query for query, _ in connection.executions
    )


def test_registration_is_disabled_before_rate_limit_database_or_hashing(
    monkeypatch,
) -> None:
    connection = RegistrationConnection()

    async def unexpected_hash(*_args):
        raise AssertionError("disabled registration must not hash")

    monkeypatch.setattr(auth.asyncio, "to_thread", unexpected_hash)
    settings = make_settings(registration_enabled=False)
    with make_client(connection, settings=settings) as client:
        capabilities = client.get("/api/auth/capabilities")
        response = client.post(
            "/api/auth/register",
            headers={"Origin": "http://admin.test"},
            json={
                "email": "new-admin@example.com",
                "password": "a-new-correct-horse-password",
            },
        )

    assert capabilities.json() == {"registration_enabled": False}
    assert response.status_code == 403
    assert response.json() == {
        "error": "registration_disabled",
        "message": "Public account registration is disabled",
    }
    assert connection.pool_acquisitions == 0
    assert connection.executions == []


def test_registration_hashes_outside_the_database_pool(monkeypatch) -> None:
    connection = RegistrationConnection()
    observed_depths: list[int] = []

    async def checked_to_thread(function, *args):
        observed_depths.append(connection.pool_depth)
        return function(*args)

    monkeypatch.setattr(auth.asyncio, "to_thread", checked_to_thread)
    with make_client(connection) as client:
        response = client.post(
            "/api/auth/register",
            headers={"Origin": "http://admin.test"},
            json={
                "email": "new-admin@example.com",
                "password": "a-new-correct-horse-password",
            },
        )

    assert response.status_code == 201
    assert observed_depths == [0]
    statements = [query for query, _ in connection.executions]
    lock_index = next(
        index for index, query in enumerate(statements)
        if "pg_advisory_xact_lock" in query
    )
    exact_count_index = next(
        index for index, query in enumerate(statements[lock_index + 1 :], lock_index + 1)
        if "SELECT count(*) FROM admin_users" in query
    )
    insert_index = next(
        index for index, query in enumerate(statements)
        if "INSERT INTO admin_users" in query
    )
    assert lock_index < exact_count_index < insert_index


def test_registration_rejects_approximate_and_locked_account_capacity(
    monkeypatch,
) -> None:
    async def unexpected_hash(*_args):
        raise AssertionError("approximate capacity must reject before hashing")

    at_capacity = RegistrationConnection(account_count=1)
    monkeypatch.setattr(auth.asyncio, "to_thread", unexpected_hash)
    with make_client(at_capacity, settings=make_settings(max_accounts=1)) as client:
        early = client.post(
            "/api/auth/register",
            headers={"Origin": "http://admin.test"},
            json={
                "email": "new-admin@example.com",
                "password": "a-new-correct-horse-password",
            },
        )

    assert early.status_code == 409
    assert early.json()["error"] == "account_capacity_reached"
    assert at_capacity.insert_attempts == 0

    raced = RegistrationConnection(account_count=0, locked_account_count=1)

    async def hash_after_preflight(function, *args):
        return function(*args)

    monkeypatch.setattr(auth.asyncio, "to_thread", hash_after_preflight)
    with make_client(raced, settings=make_settings(max_accounts=1)) as client:
        locked = client.post(
            "/api/auth/register",
            headers={"Origin": "http://admin.test"},
            json={
                "email": "new-admin@example.com",
                "password": "a-new-correct-horse-password",
            },
        )

    assert locked.status_code == 409
    assert locked.json()["error"] == "account_capacity_reached"
    assert raced.insert_attempts == 0
    assert not any(
        "INSERT INTO admin_sessions" in query for query, _ in raced.executions
    )


def test_registration_consumes_the_shared_auth_rate_limit() -> None:
    connection = RegistrationConnection()
    settings = make_settings(login_global_rate_limit=1)
    with make_client(connection, settings=settings) as client:
        first = client.post(
            "/api/auth/register",
            headers={"Origin": "http://admin.test"},
            json={
                "email": "new-admin@example.com",
                "password": "a-new-correct-horse-password",
            },
        )
        second = client.post(
            "/api/auth/register",
            headers={"Origin": "http://admin.test"},
            json={
                "email": "another-admin@example.com",
                "password": "a-new-correct-horse-password",
            },
        )

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["error"] == "auth_throttled"
    assert second.headers["retry-after"] == "60"


def test_registration_rejects_unknown_fields_and_cross_site_origin() -> None:
    connection = RegistrationConnection()
    with make_client(connection) as client:
        unknown = client.post(
            "/api/auth/register",
            headers={"Origin": "http://admin.test"},
            json={
                "email": "new-admin@example.com",
                "password": "a-new-correct-horse-password",
                "project_id": "other",
            },
        )
        cross_site = client.post(
            "/api/auth/register",
            headers={"Origin": "https://attacker.example"},
            json={
                "email": "new-admin@example.com",
                "password": "a-new-correct-horse-password",
            },
        )

    assert unknown.status_code == 422
    assert cross_site.status_code == 403
    assert connection.insert_attempts == 0


class EmptyProjectSessionConnection:
    def __init__(self, raw_token: str) -> None:
        self.raw_token = raw_token
        self.user_id = uuid.UUID("50000000-0000-4000-8000-000000000005")

    async def fetchrow(self, query: str, *args):
        assert "FROM admin_sessions" in query
        digest = hashlib.sha256(self.raw_token.encode()).hexdigest()
        assert args[0] == digest
        return {
            "session_id": uuid.UUID("60000000-0000-4000-8000-000000000006"),
            "token_hash": digest,
            "csrf_hash": "c" * 64,
            "user_id": self.user_id,
            "email": "new-admin@example.com",
        }

    async def fetch(self, query: str, *args):
        assert "FROM admin_user_projects" in query
        assert args == (self.user_id,)
        return []

    async def execute(self, query: str, *args):
        assert "UPDATE admin_sessions" in query
        return "OK"


def test_zero_project_user_remains_authenticated() -> None:
    raw_token = "opaque-zero-project-session"
    connection = EmptyProjectSessionConnection(raw_token)
    with make_client(connection) as client:
        client.cookies.set("apdl_admin_session", raw_token, path="/api")
        response = client.get("/api/auth/me")

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(connection.user_id),
        "email": "new-admin@example.com",
        "projects": [],
    }
