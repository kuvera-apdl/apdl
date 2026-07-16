from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import projects
from app.auth import AdminSession, require_session
from app.security import token_hash
from conftest import make_settings


class ProjectConnection:
    def __init__(self, *, exists: bool = False) -> None:
        self.exists = exists
        self.statements: list[tuple[str, tuple[object, ...]]] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchval(self, query: str, *args):
        self.statements.append((query, args))
        return None if self.exists else args[0]

    async def execute(self, query: str, *args):
        self.statements.append((query, args))
        return "OK"


class ProjectPool:
    def __init__(self, connection: ProjectConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def make_client(connection: ProjectConnection, session: AdminSession) -> TestClient:
    app = FastAPI()
    app.state.settings = make_settings()
    app.state.pg_pool = ProjectPool(connection)
    app.include_router(projects.router)
    app.dependency_overrides[require_session] = lambda: session
    return TestClient(app)


def zero_project_session(csrf: str) -> AdminSession:
    return AdminSession(
        session_id="10000000-0000-4000-8000-000000000001",
        token_hash="a" * 64,
        csrf_hash=token_hash(csrf),
        user_id="20000000-0000-4000-8000-000000000002",
        email="admin@example.com",
        projects={},
    )


def test_zero_project_user_creates_project_and_receives_owner_roles() -> None:
    csrf = "project-csrf-token"
    connection = ProjectConnection()
    with make_client(connection, zero_project_session(csrf)) as client:
        client.cookies.set("apdl_admin_csrf", csrf, path="/")
        response = client.post(
            "/api/projects",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"project_id": "newproject"},
        )

    assert response.status_code == 201
    identity = response.json()
    assert identity["email"] == "admin@example.com"
    assert identity["projects"] == [
        {
            "project_id": "newproject",
            "roles": sorted(projects.PROJECT_CREATOR_ROLES),
        }
    ]
    membership = next(
        statement
        for statement in connection.statements
        if "INSERT INTO admin_user_projects" in statement[0]
    )
    assert membership[1][1:] == (
        "newproject",
        list(projects.PROJECT_CREATOR_ROLES),
    )


def test_self_registered_project_roles_are_core_only() -> None:
    assert projects.PROJECT_CREATOR_ROLES == (
        "events:write",
        "config:read",
        "config:write",
        "config:evaluate",
        "query:read",
        "agents:read",
    )
    assert not {
        "agents:run",
        "agents:manage",
        "agents:approve",
    }.intersection(projects.PROJECT_CREATOR_ROLES)


def test_project_creation_preserves_existing_profile_projects() -> None:
    csrf = "project-csrf-token"
    session = zero_project_session(csrf)
    session = AdminSession(
        **{
            **session.__dict__,
            "projects": {"existing": frozenset({"config:read"})},
        }
    )
    connection = ProjectConnection()
    with make_client(connection, session) as client:
        client.cookies.set("apdl_admin_csrf", csrf, path="/")
        response = client.post(
            "/api/projects",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"project_id": "second"},
        )

    assert response.status_code == 201
    assert [item["project_id"] for item in response.json()["projects"]] == [
        "existing",
        "second",
    ]


def test_project_creation_rejects_duplicates_and_cross_site_requests() -> None:
    csrf = "project-csrf-token"
    session = zero_project_session(csrf)
    duplicate_connection = ProjectConnection(exists=True)
    with make_client(duplicate_connection, session) as client:
        client.cookies.set("apdl_admin_csrf", csrf, path="/")
        duplicate = client.post(
            "/api/projects",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"project_id": "existing"},
        )
        cross_site = client.post(
            "/api/projects",
            headers={"Origin": "https://attacker.example", "X-CSRF-Token": csrf},
            json={"project_id": "attacker"},
        )

    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Project ID already exists"}
    assert cross_site.status_code == 403
    assert not any(
        "admin_user_projects" in query for query, _ in duplicate_connection.statements
    )


def test_project_creation_requires_strict_schema_and_csrf() -> None:
    csrf = "project-csrf-token"
    connection = ProjectConnection()
    with make_client(connection, zero_project_session(csrf)) as client:
        invalid_id = client.post(
            "/api/projects",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"project_id": "not-valid"},
        )
        unknown_field = client.post(
            "/api/projects",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"project_id": "valid", "owner": "caller"},
        )
        missing_csrf = client.post(
            "/api/projects",
            headers={"Origin": "http://admin.test"},
            json={"project_id": "valid"},
        )

    assert invalid_id.status_code == 422
    assert unknown_field.status_code == 422
    assert missing_csrf.status_code == 403
    assert connection.statements == []
