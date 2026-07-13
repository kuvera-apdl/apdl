from __future__ import annotations

import json
import hashlib
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
