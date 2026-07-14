from __future__ import annotations

import json

import httpx
import pytest

from app.auth import AdminSession
from app.security import token_hash
from conftest import TEST_API_KEY, proxy_client


@pytest.mark.asyncio
async def test_proxy_injects_server_key_and_discards_caller_credentials(
    admin_session: AdminSession,
) -> None:
    seen: dict[str, str | None] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-api-key")
        seen["cookie"] = request.headers.get("cookie")
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"flags": []})

    async with proxy_client(httpx.MockTransport(upstream), admin_session) as client:
        response = client.get(
            "/api/projects/demo/config/v1/flags",
            headers={
                "X-API-Key": "attacker-controlled",
                "Authorization": "Bearer attacker-controlled",
                "Cookie": "untrusted=value",
            },
        )

    assert response.status_code == 200
    assert seen == {"key": TEST_API_KEY, "cookie": None, "authorization": None}


@pytest.mark.asyncio
async def test_proxy_rejects_credentials_in_the_query_string(
    admin_session: AdminSession,
) -> None:
    called = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    async with proxy_client(httpx.MockTransport(upstream), admin_session) as client:
        response = client.get(
            "/api/projects/demo/config/v1/stream?api_key=browser-secret"
        )

    assert response.status_code == 400
    assert not called


@pytest.mark.asyncio
async def test_proxy_hides_projects_outside_the_session(
    admin_session: AdminSession,
) -> None:
    called = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    async with proxy_client(httpx.MockTransport(upstream), admin_session) as client:
        response = client.get("/api/projects/other/config/v1/flags")

    assert response.status_code == 404
    assert not called


@pytest.mark.asyncio
async def test_proxy_requires_role_before_calling_upstream(
    admin_session: AdminSession,
) -> None:
    restricted = AdminSession(
        **{
            **admin_session.__dict__,
            "projects": {"demo": frozenset({"config:read"})},
        }
    )
    called = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    async with proxy_client(httpx.MockTransport(upstream), restricted) as client:
        response = client.post(
            "/api/projects/demo/config/v1/admin/flags",
            headers={"Origin": "http://admin.test"},
            json={"key": "test"},
        )

    assert response.status_code == 403
    assert not called


@pytest.mark.asyncio
async def test_proxy_does_not_expose_global_repository_onboarding(
    admin_session: AdminSession,
) -> None:
    called = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    async with proxy_client(httpx.MockTransport(upstream), admin_session) as client:
        repositories = client.get("/api/projects/demo/codegen/v1/github/repos")
        connect = client.post(
            "/api/projects/demo/codegen/v1/connections",
            headers={"Origin": "http://admin.test"},
            json={"project_id": "demo", "repo": "other-tenant/secret"},
        )

    assert repositories.status_code == 404
    assert connect.status_code == 404
    assert not called


@pytest.mark.asyncio
async def test_proxy_validates_csrf_and_project_assertions(
    admin_session: AdminSession,
) -> None:
    csrf = "csrf-token"
    session = AdminSession(
        **{
            **admin_session.__dict__,
            "csrf_hash": token_hash(csrf),
        }
    )
    bodies: list[dict] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json={"accepted": 1})

    async with proxy_client(httpx.MockTransport(upstream), session) as client:
        missing_csrf = client.post(
            "/api/projects/demo/ingestion/v1/events",
            headers={"Origin": "http://admin.test"},
            json={"events": []},
        )
        client.cookies.set("apdl_admin_csrf", csrf, path="/api")
        mismatch = client.post(
            "/api/projects/demo/ingestion/v1/events",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"project_id": "other", "events": []},
        )
        accepted = client.post(
            "/api/projects/demo/ingestion/v1/events",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"project_id": "demo", "events": []},
        )
        audit_statements = client.app.state.audit_statements

    assert missing_csrf.status_code == 403
    assert mismatch.status_code == 403
    assert accepted.status_code == 202
    assert bodies == [{"project_id": "demo", "events": []}]
    insert = next(
        statement for statement in audit_statements if "INSERT INTO" in statement[0]
    )
    completed = next(
        statement for statement in audit_statements if "UPDATE" in statement[0]
    )
    assert str(insert[1][1]) == "20000000-0000-4000-8000-000000000002"
    assert insert[1][2:8] == (
        "admin@example.com",
        "demo",
        "events:write",
        "ingestion",
        "POST",
        "/v1/events",
    )
    assert completed[1][1] == 202
    assert "{'events':" not in repr(insert[1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "media_type",
    [
        "application/merge-patch+json",
        "application/vnd.apdl+json",
        "text/json",
    ],
)
async def test_codegen_proxy_rejects_noncanonical_json_media_types(
    admin_session: AdminSession,
    media_type: str,
) -> None:
    csrf = "csrf-token"
    session = AdminSession(
        **{
            **admin_session.__dict__,
            "csrf_hash": token_hash(csrf),
        }
    )
    called = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(201, json={"changeset_id": "changeset-1"})

    async with proxy_client(httpx.MockTransport(upstream), session) as client:
        client.cookies.set("apdl_admin_csrf", csrf, path="/api")
        response = client.post(
            "/api/projects/demo/codegen/v1/changesets",
            headers={
                "Origin": "http://admin.test",
                "X-CSRF-Token": csrf,
                "Content-Type": media_type,
            },
            content=json.dumps({"project_id": "other"}),
        )

    assert response.status_code == 415
    assert response.json() == {
        "detail": "Codegen request bodies must use application/json"
    }
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changeset_path",
    [
        "/v1/changesets/cs_other",
        "/v1/changesets/cs_other/observations",
        "/v1/changesets/cs_other/runtime-observations",
        "/v1/changesets/cs_other/future-child-resource",
    ],
)
async def test_codegen_proxy_hides_every_cross_tenant_changeset_child_route(
    admin_session: AdminSession,
    changeset_path: str,
) -> None:
    seen_paths: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200, json={"project_id": "other"})

    async with proxy_client(httpx.MockTransport(upstream), admin_session) as client:
        response = client.get(f"/api/projects/demo/codegen{changeset_path}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Changeset not found"}
    assert seen_paths == ["/v1/changesets/cs_other"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "child_path",
    ["observations", "runtime-observations"],
)
async def test_codegen_proxy_forwards_authorized_changeset_child_routes(
    admin_session: AdminSession,
    child_path: str,
) -> None:
    seen_paths: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/changesets/cs_demo":
            return httpx.Response(200, json={"project_id": "demo"})
        return httpx.Response(200, json={"observations": []})

    async with proxy_client(httpx.MockTransport(upstream), admin_session) as client:
        response = client.get(
            f"/api/projects/demo/codegen/v1/changesets/cs_demo/{child_path}"
        )

    assert response.status_code == 200
    assert seen_paths == [
        "/v1/changesets/cs_demo",
        f"/v1/changesets/cs_demo/{child_path}",
    ]
