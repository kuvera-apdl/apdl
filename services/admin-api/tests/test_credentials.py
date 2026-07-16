from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import credentials
from app.auth import AdminSession, require_session
from app.security import token_hash
from conftest import make_settings

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
REVOKED = datetime(2026, 7, 16, 13, 0, tzinfo=timezone.utc)


class CredentialConnection:
    def __init__(self, membership_roles: set[str] | None = None) -> None:
        self.membership_roles = membership_roles
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.credentials: dict[str, dict[str, object]] = {}
        self.pending_insert: dict[str, object] | None = None
        self.audits: list[dict[str, object]] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, query: str, *args):
        self.calls.append((query, args))
        if "SELECT membership.roles" in query:
            if self.membership_roles is None:
                return None
            return {"roles": sorted(self.membership_roles)}
        if "FROM admin_managed_credentials AS managed" in query:
            return self.credentials.get(str(args[1]))
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query: str, *args):
        self.calls.append((query, args))
        if "FROM admin_credential_audit" in query:
            credential_id = str(args[1])
            return [
                row
                for row in self.audits
                if row["credential_id"] == credential_id
                or row["successor_credential_id"] == credential_id
            ]
        if "FROM admin_managed_credentials AS managed" in query:
            return list(self.credentials.values())
        raise AssertionError(f"Unexpected fetch query: {query}")

    async def fetchval(self, query: str, *args):
        self.calls.append((query, args))
        if "INSERT INTO admin_managed_credentials" in query:
            assert self.pending_insert is not None
            row = {
                **self.pending_insert,
                "created_at": NOW,
                "revoked_at": None,
                "active": True,
                "rotated_from_credential_id": args[4],
            }
            self.credentials[str(args[0])] = row
            return NOW
        if "SELECT EXISTS" in query:
            return any(
                row["rotated_from_credential_id"] == args[0]
                for row in self.credentials.values()
            )
        if "UPDATE auth_credentials" in query:
            row = self.credentials.get(str(args[0]))
            if row is None or not row["active"]:
                return None
            row["active"] = False
            row["revoked_at"] = REVOKED
            return REVOKED
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def execute(self, query: str, *args):
        self.calls.append((query, args))
        if "INSERT INTO auth_credentials" in query:
            self.pending_insert = {
                "credential_id": str(args[0]),
                "project_id": str(args[1]),
                "credential_kind": str(args[2]),
                "key_prefix": str(args[3]),
                "roles": list(args[5]),
            }
        elif "INSERT INTO admin_credential_audit" in query:
            self.audits.append(
                {
                    "audit_id": args[0],
                    "project_id": args[1],
                    "credential_id": args[2],
                    "action": args[3],
                    "actor_user_id": args[4],
                    "actor_email": args[5],
                    "credential_kind": args[6],
                    "roles": list(args[7]),
                    "successor_credential_id": args[8],
                    "created_at": NOW,
                }
            )
        return "OK"


class CredentialPool:
    def __init__(self, connection: CredentialConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def managed_session(csrf: str) -> AdminSession:
    return AdminSession(
        session_id="10000000-0000-4000-8000-000000000001",
        token_hash="a" * 64,
        csrf_hash=token_hash(csrf),
        user_id="20000000-0000-4000-8000-000000000002",
        email="admin@example.com",
        projects={"demo": frozenset({"credentials:manage"})},
    )


def make_client(
    connection: CredentialConnection,
    session: AdminSession,
) -> TestClient:
    app = FastAPI()
    app.state.settings = make_settings()
    app.state.pg_pool = CredentialPool(connection)
    app.include_router(credentials.router)
    app.dependency_overrides[require_session] = lambda: session
    return TestClient(app)


def authorize(client: TestClient, csrf: str) -> None:
    client.cookies.set("apdl_admin_csrf", csrf, path="/")


def test_create_browser_credential_reveals_once_and_persists_only_hash() -> None:
    csrf = "credential-csrf"
    connection = CredentialConnection(
        {"credentials:manage", "events:write", "config:read"}
    )
    with make_client(connection, managed_session(csrf)) as client:
        authorize(client, csrf)
        response = client.post(
            "/api/projects/demo/credentials",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={
                "credential_kind": "browser",
                "roles": ["events:write", "config:read"],
            },
        )
        listed = client.get("/api/projects/demo/credentials")

    assert response.status_code == 201
    revealed = response.json()
    assert revealed["api_key"].startswith("client_demo_")
    assert listed.status_code == 200
    assert "api_key" not in listed.json()[0]
    auth_insert = next(
        call for call in connection.calls if "INSERT INTO auth_credentials" in call[0]
    )
    assert len(str(auth_insert[1][4])) == 64
    assert "actor_user_id" not in auth_insert[0]
    assert UUID("20000000-0000-4000-8000-000000000002") not in auth_insert[1]
    assert revealed["api_key"] not in repr(connection.calls)
    list_query = next(
        query
        for query, _ in connection.calls
        if "ORDER BY managed.created_at DESC" in query
    )
    assert "FROM admin_managed_credentials AS managed" in list_query


def test_create_enforces_strict_kind_scope_and_current_membership() -> None:
    csrf = "credential-csrf"
    connection = CredentialConnection(
        {"credentials:manage", "events:write", "config:read"}
    )
    with make_client(connection, managed_session(csrf)) as client:
        authorize(client, csrf)
        invalid_browser = client.post(
            "/api/projects/demo/credentials",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"credential_kind": "browser", "roles": ["events:write"]},
        )
        overbroad = client.post(
            "/api/projects/demo/credentials",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={
                "credential_kind": "confidential",
                "roles": ["events:write", "query:read"],
            },
        )

    assert invalid_browser.status_code == 422
    assert overbroad.status_code == 403
    assert not any(
        "INSERT INTO auth_credentials" in query for query, _ in connection.calls
    )


def test_stale_session_role_cannot_manage_credentials_after_database_revocation() -> None:
    csrf = "credential-csrf"
    connection = CredentialConnection({"events:write", "config:read"})
    with make_client(connection, managed_session(csrf)) as client:
        authorize(client, csrf)
        response = client.post(
            "/api/projects/demo/credentials",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={
                "credential_kind": "browser",
                "roles": ["events:write", "config:read"],
            },
        )

    assert response.status_code == 403
    assert not any(
        "INSERT INTO auth_credentials" in query for query, _ in connection.calls
    )


def test_rotation_creates_one_successor_and_leaves_predecessor_active() -> None:
    csrf = "credential-csrf"
    connection = CredentialConnection(
        {
            "credentials:manage",
            "events:write",
            "config:read",
            "query:read",
        }
    )
    predecessor_id = "managed-" + "1" * 32
    connection.credentials[predecessor_id] = {
        "credential_id": predecessor_id,
        "project_id": "demo",
        "credential_kind": "confidential",
        "key_prefix": "proj_demo_",
        "roles": ["events:write", "query:read"],
        "active": True,
        "created_at": NOW,
        "revoked_at": None,
        "rotated_from_credential_id": None,
    }
    with make_client(connection, managed_session(csrf)) as client:
        authorize(client, csrf)
        unknown_field = client.post(
            f"/api/projects/demo/credentials/{predecessor_id}/rotate",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"roles": ["events:write"]},
        )
        response = client.post(
            f"/api/projects/demo/credentials/{predecessor_id}/rotate",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={},
        )

    assert unknown_field.status_code == 422
    assert response.status_code == 201
    successor = response.json()
    assert successor["credential_id"] != predecessor_id
    assert successor["rotated_from_credential_id"] == predecessor_id
    assert connection.credentials[predecessor_id]["active"] is True
    assert not any(
        "UPDATE auth_credentials" in query for query, _ in connection.calls
    )
    assert connection.audits[-1]["action"] == "rotate"
    assert connection.audits[-1]["successor_credential_id"] == successor["credential_id"]


def test_revoke_and_audit_are_project_scoped_and_do_not_reveal_secret() -> None:
    csrf = "credential-csrf"
    connection = CredentialConnection({"credentials:manage"})
    credential_id = "managed-" + "2" * 32
    connection.credentials[credential_id] = {
        "credential_id": credential_id,
        "project_id": "demo",
        "credential_kind": "browser",
        "key_prefix": "client_demo_",
        "roles": ["events:write", "config:read"],
        "active": True,
        "created_at": NOW,
        "revoked_at": None,
        "rotated_from_credential_id": None,
    }
    with make_client(connection, managed_session(csrf)) as client:
        authorize(client, csrf)
        revoked = client.post(
            f"/api/projects/demo/credentials/{credential_id}/revoke",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={},
        )
        audit = client.get(
            f"/api/projects/demo/credentials/{credential_id}/audit"
        )

    assert revoked.status_code == 200
    assert revoked.json()["active"] is False
    assert revoked.json()["revoked_at"] == REVOKED.isoformat().replace("+00:00", "Z")
    assert "api_key" not in revoked.json()
    assert audit.status_code == 200
    assert [entry["action"] for entry in audit.json()] == ["revoke"]
    assert audit.json()[0]["actor_user_id"] == str(
        UUID("20000000-0000-4000-8000-000000000002")
    )
