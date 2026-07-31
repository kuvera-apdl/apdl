from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import projects
from app.auth import AdminSession, require_session
from app.security import token_hash
from conftest import make_settings

OWNER_ID = UUID("20000000-0000-4000-8000-000000000002")
TARGET_ID = UUID("30000000-0000-4000-8000-000000000003")
CREATOR_ID = UUID("40000000-0000-4000-8000-000000000004")


class OwnershipConnection:
    def __init__(
        self,
        *,
        actor_id: UUID = OWNER_ID,
        owner_id: UUID | None = OWNER_ID,
        target_roles: list[str] | None = None,
    ) -> None:
        self.actor_id = actor_id
        self.owner_id = owner_id
        self.target_roles = target_roles or ["config:read", "members:manage"]
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.audits: list[tuple[object, ...]] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, query: str, *args):
        self.statements.append((query, args))
        if "execution.authorization_source" in query:
            return {
                "project_id": "demo",
                "created_by": CREATOR_ID,
                "owner_user_id": self.owner_id,
                "creator_email": "creator@example.com",
                "owner_email": (
                    "target@example.com"
                    if self.owner_id == TARGET_ID
                    else "owner@example.com"
                ),
                "execution_authorization_source": "operator_provisioned",
            }
        if "SELECT project.owner_user_id" in query:
            return {"owner_user_id": self.owner_id}
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query: str, *args):
        self.statements.append((query, args))
        if "FROM admin_project_ownership_audit" in query:
            return [
                {
                    "audit_id": UUID(
                        "60000000-0000-4000-8000-000000000006"
                    ),
                    "project_id": "demo",
                    "previous_owner_user_id": OWNER_ID,
                    "previous_owner_email": "owner@example.com",
                    "new_owner_user_id": TARGET_ID,
                    "new_owner_email": "target@example.com",
                    "actor": str(OWNER_ID),
                    "reason": "Human owner transfer",
                    "created_at": datetime(2026, 7, 30, tzinfo=timezone.utc),
                }
            ]
        assert "FOR UPDATE OF membership, account" in query
        return [
            {
                "user_id": self.actor_id,
                "roles": ["members:manage"],
                "active": True,
            },
            {
                "user_id": TARGET_ID,
                "roles": self.target_roles,
                "active": True,
            },
        ]

    async def fetchval(self, query: str, *args):
        self.statements.append((query, args))
        assert "'members:manage' = ANY(membership.roles)" in query
        return self.actor_id

    async def execute(self, query: str, *args):
        self.statements.append((query, args))
        if "UPDATE admin_projects" in query:
            if self.owner_id != args[1]:
                return "UPDATE 0"
            self.owner_id = args[2]
            return "UPDATE 1"
        if "INSERT INTO admin_project_ownership_audit" in query:
            self.audits.append(args)
        return "OK"


class OwnershipPool:
    def __init__(self, connection: OwnershipConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def _session(csrf: str, *, user_id: UUID = OWNER_ID) -> AdminSession:
    return AdminSession(
        session_id="10000000-0000-4000-8000-000000000001",
        token_hash="a" * 64,
        csrf_hash=token_hash(csrf),
        user_id=str(user_id),
        email="owner@example.com",
        projects={"demo": frozenset({"config:read", "members:manage"})},
    )


def _client(connection: OwnershipConnection, session: AdminSession) -> TestClient:
    app = FastAPI()
    app.state.settings = make_settings()
    app.state.pg_pool = OwnershipPool(connection)
    app.include_router(projects.router)
    app.dependency_overrides[require_session] = lambda: session
    return TestClient(app)


def test_project_member_can_read_ownership_and_execution_authorization() -> None:
    csrf = "ownership-csrf"
    connection = OwnershipConnection()
    with _client(connection, _session(csrf)) as client:
        response = client.get("/api/projects/demo/authorization")

    assert response.status_code == 200
    assert response.json() == {
        "project_id": "demo",
        "creator": {
            "user_id": str(CREATOR_ID),
            "email": "creator@example.com",
        },
        "ownership": {
            "kind": "human",
            "owner_user_id": str(OWNER_ID),
            "owner_email": "owner@example.com",
        },
        "execution_authorization": {
            "authorized": True,
            "source": "operator_provisioned",
        },
    }


def test_owner_transfers_only_owner_column_and_writes_immutable_audit() -> None:
    csrf = "ownership-csrf"
    connection = OwnershipConnection()
    with _client(connection, _session(csrf)) as client:
        client.cookies.set("apdl_admin_csrf", csrf, path="/")
        response = client.post(
            "/api/projects/demo/ownership/transfer",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"target_user_id": str(TARGET_ID)},
        )

    assert response.status_code == 200
    assert response.json()["ownership"] == {
        "kind": "human",
        "owner_user_id": str(TARGET_ID),
        "owner_email": "target@example.com",
    }
    assert len(connection.audits) == 1
    assert connection.audits[0][2:5] == (
        OWNER_ID,
        TARGET_ID,
        str(OWNER_ID),
    )
    sql = [" ".join(query.split()) for query, _ in connection.statements]
    assert any("FOR UPDATE OF project" in query for query in sql)
    assert any("FOR UPDATE OF membership, account" in query for query in sql)
    assert not any(
        "UPDATE admin_project_execution_authorizations" in query for query in sql
    )


def test_operator_managed_project_cannot_be_claimed_from_human_api() -> None:
    csrf = "ownership-csrf"
    connection = OwnershipConnection(owner_id=None)
    with _client(connection, _session(csrf)) as client:
        client.cookies.set("apdl_admin_csrf", csrf, path="/")
        response = client.post(
            "/api/projects/demo/ownership/transfer",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"target_user_id": str(TARGET_ID)},
        )

    assert response.status_code == 409
    assert "cannot be claimed" in response.json()["detail"]
    assert connection.audits == []


def test_manager_can_read_immutable_ownership_history() -> None:
    csrf = "ownership-csrf"
    connection = OwnershipConnection()
    with _client(connection, _session(csrf)) as client:
        response = client.get("/api/projects/demo/ownership/audit")

    assert response.status_code == 200
    assert response.json() == [
        {
            "audit_id": "60000000-0000-4000-8000-000000000006",
            "project_id": "demo",
            "previous_owner_user_id": str(OWNER_ID),
            "previous_owner_email": "owner@example.com",
            "new_owner_user_id": str(TARGET_ID),
            "new_owner_email": "target@example.com",
            "actor": str(OWNER_ID),
            "reason": "Human owner transfer",
            "created_at": "2026-07-30T00:00:00Z",
        }
    ]


def test_non_owner_and_ineligible_target_cannot_transfer() -> None:
    csrf = "ownership-csrf"
    non_owner = UUID("50000000-0000-4000-8000-000000000005")
    connection = OwnershipConnection(actor_id=non_owner)
    with _client(connection, _session(csrf, user_id=non_owner)) as client:
        client.cookies.set("apdl_admin_csrf", csrf, path="/")
        response = client.post(
            "/api/projects/demo/ownership/transfer",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"target_user_id": str(TARGET_ID)},
        )
    assert response.status_code == 403

    connection = OwnershipConnection(target_roles=["config:read"])
    with _client(connection, _session(csrf)) as client:
        client.cookies.set("apdl_admin_csrf", csrf, path="/")
        response = client.post(
            "/api/projects/demo/ownership/transfer",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"target_user_id": str(TARGET_ID)},
        )
    assert response.status_code == 409
    assert connection.audits == []


def test_transfer_requires_strict_schema_origin_and_csrf() -> None:
    csrf = "ownership-csrf"
    connection = OwnershipConnection()
    with _client(connection, _session(csrf)) as client:
        unknown = client.post(
            "/api/projects/demo/ownership/transfer",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"target_user_id": str(TARGET_ID), "force": True},
        )
        missing_csrf = client.post(
            "/api/projects/demo/ownership/transfer",
            headers={"Origin": "http://admin.test"},
            json={"target_user_id": str(TARGET_ID)},
        )

    assert unknown.status_code == 422
    assert missing_csrf.status_code == 403
