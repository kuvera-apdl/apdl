from __future__ import annotations

import hashlib
import json
import re

import httpx
import pytest

from app.auth import AdminSession
from app.security import token_hash
from conftest import TEST_API_KEY, make_settings, proxy_client


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
async def test_proxy_mints_and_removes_ephemeral_key_for_dynamic_project(
    admin_session: AdminSession,
) -> None:
    seen_key = ""

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal seen_key
        seen_key = request.headers["x-api-key"]
        return httpx.Response(200, json={"flags": []})

    settings = make_settings(service_api_keys={})
    async with proxy_client(
        httpx.MockTransport(upstream), admin_session, settings
    ) as client:
        response = client.get("/api/projects/demo/config/v1/flags")
        statements = client.app.state.audit_statements

    assert response.status_code == 200
    assert re.fullmatch(r"proj_demo_[0-9a-f]{48}", seen_key)
    insert = next(
        statement
        for statement in statements
        if "INSERT INTO auth_credentials" in statement[0]
    )
    credential_id = insert[1][0]
    assert insert[1][1] == "demo"
    assert insert[1][2] == hashlib.sha256(seen_key.encode()).hexdigest()
    assert insert[1][3] == sorted(admin_session.projects["demo"])
    assert insert[1][4] == 300
    removal = next(
        statement
        for statement in statements
        if "DELETE FROM auth_credentials WHERE credential_id = $1" in statement[0]
    )
    assert removal[1] == (credential_id,)


@pytest.mark.asyncio
async def test_codegen_proxy_uses_project_scoped_service_key(
    admin_session: AdminSession,
) -> None:
    seen: list[tuple[str | None, str | None]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.headers.get("x-api-key"),
                request.headers.get("x-apdl-internal-token"),
            )
        )
        return httpx.Response(200, json=[])

    async with proxy_client(httpx.MockTransport(upstream), admin_session) as client:
        response = client.get(
            "/api/projects/demo/codegen/v1/changesets?project_id=demo"
        )

    assert response.status_code == 200
    assert seen == [(TEST_API_KEY, None)]


@pytest.mark.asyncio
async def test_codegen_proxy_reuses_ephemeral_project_key_for_scope_and_forward(
    admin_session: AdminSession,
) -> None:
    seen_keys: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers["x-api-key"])
        if request.url.path == "/v1/changesets/cs_demo":
            return httpx.Response(200, json={"project_id": "demo"})
        return httpx.Response(200, json={"observations": []})

    settings = make_settings(service_api_keys={})
    async with proxy_client(
        httpx.MockTransport(upstream), admin_session, settings
    ) as client:
        response = client.get(
            "/api/projects/demo/codegen/v1/changesets/cs_demo/observations"
        )
        statements = client.app.state.audit_statements

    assert response.status_code == 200
    assert len(seen_keys) == 2
    assert seen_keys[0] == seen_keys[1]
    assert re.fullmatch(r"proj_demo_[0-9a-f]{48}", seen_keys[0])
    inserts = [
        statement
        for statement in statements
        if "INSERT INTO auth_credentials" in statement[0]
    ]
    assert len(inserts) == 1
    removal = next(
        statement
        for statement in statements
        if "DELETE FROM auth_credentials WHERE credential_id = $1" in statement[0]
    )
    assert removal[1] == (inserts[0][1][0],)


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
async def test_codegen_proxy_hides_project_forbidden_changeset_as_not_found(
    admin_session: AdminSession,
) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/changesets/cs_other"
        return httpx.Response(status_code=403)

    async with proxy_client(httpx.MockTransport(upstream), admin_session) as client:
        response = client.get(
            "/api/projects/demo/codegen/v1/changesets/cs_other/observations"
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "Changeset not found"}


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
