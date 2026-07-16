import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.auth import (
    PostgresAuthenticator,
    Principal,
    authenticate_request,
    credential_has_current_role,
)
from app.main import app


API_KEY = "proj_verifiedproject_0123456789abcdef0123456789abcdef"
BROWSER_KEY = "client_verifiedproject_0123456789abcdef0123456789abcdef"


class FakeConnection:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.row

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return self.row


class Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, row):
        self.connection = FakeConnection(row)

    def acquire(self):
        return Acquire(self.connection)


def credential_row(api_key=API_KEY, **overrides):
    row = {
        "credential_id": "credential-1",
        "project_id": "verifiedproject",
        "credential_kind": "confidential",
        "key_prefix": "proj_verifiedproject_",
        "key_hash": hashlib.sha256(api_key.encode()).hexdigest(),
        "roles": ["query:read", "events:write"],
        "active": True,
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    row.update(overrides)
    return row


def browser_credential_row(**overrides):
    row = credential_row(api_key=BROWSER_KEY)
    row.update({
        "credential_kind": "browser",
        "key_prefix": "client_verifiedproject_",
        "roles": ["events:write", "config:read"],
    })
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_authentication_derives_authority_from_stored_record():
    pool = FakePool(credential_row())
    principal = await PostgresAuthenticator(pool).authenticate(API_KEY)

    assert principal is not None
    assert principal.project_id == "verifiedproject"
    assert principal.credential_id == "credential-1"
    assert principal.roles == frozenset({"query:read", "events:write"})
    query, args = pool.connection.calls[0]
    assert "WHERE key_hash = $1" in query
    assert "project_id =" not in query
    assert args == (hashlib.sha256(API_KEY.encode()).hexdigest(),)


@pytest.mark.asyncio
async def test_syntactically_valid_unregistered_key_is_rejected():
    principal = await PostgresAuthenticator(FakePool(None)).authenticate(API_KEY)
    assert principal is None


@pytest.mark.asyncio
async def test_browser_credential_is_accepted_at_its_exact_role_ceiling():
    principal = await PostgresAuthenticator(
        FakePool(browser_credential_row())
    ).authenticate(BROWSER_KEY)

    assert principal is not None
    assert principal.project_id == "verifiedproject"
    assert principal.roles == frozenset({"events:write", "config:read"})


@pytest.mark.asyncio
async def test_authentication_rejects_misprovisioned_project_hint():
    principal = await PostgresAuthenticator(
        FakePool(credential_row(project_id="otherproject"))
    ).authenticate(API_KEY)
    assert principal is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_key", "row"),
    [
        (API_KEY, credential_row(credential_kind="browser")),
        (API_KEY, credential_row(key_prefix="client_verifiedproject_")),
        (
            BROWSER_KEY,
            browser_credential_row(roles=["events:write", "config:write"]),
        ),
        (BROWSER_KEY, browser_credential_row(roles=["config:read"])),
        (
            BROWSER_KEY,
            browser_credential_row(roles=["events:write", "config:read", "query:read"]),
        ),
        (
            BROWSER_KEY,
            browser_credential_row(
                roles=["events:write", "config:read", "config:read"]
            ),
        ),
        (API_KEY, credential_row(roles=["not:a:role"])),
    ],
)
async def test_authentication_rejects_kind_prefix_or_role_drift(api_key, row):
    principal = await PostgresAuthenticator(FakePool(row)).authenticate(api_key)
    assert principal is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"active": False},
        {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
        {"key_hash": "0" * 64},
    ],
)
async def test_authentication_rejects_revoked_expired_or_wrong_key(overrides):
    principal = await PostgresAuthenticator(
        FakePool(credential_row(**overrides))
    ).authenticate(API_KEY)
    assert principal is None


@pytest.mark.asyncio
async def test_established_credential_role_is_revalidated_by_stored_identity():
    principal = Principal(
        credential_id="credential-1",
        project_id="verifiedproject",
        roles=frozenset({"config:read"}),
    )
    pool = FakePool(True)

    assert await credential_has_current_role(pool, principal, "config:read")

    query, args = pool.connection.calls[0]
    assert "credential_id = $1" in query
    assert "project_id = $2" in query
    assert "active" in query
    assert "revoked_at IS NULL" in query
    assert "(expires_at IS NULL OR expires_at > NOW())" in query
    assert "$3::TEXT = ANY(roles)" in query
    assert args == ("credential-1", "verifiedproject", "config:read")


@pytest.mark.asyncio
async def test_authentication_dependency_fails_closed_when_registry_is_unavailable():
    class FailingAuthenticator:
        async def authenticate(self, api_key):
            raise ConnectionError("database unavailable")

    request = SimpleNamespace(
        headers={"x-api-key": API_KEY},
        query_params={},
        app=SimpleNamespace(
            state=SimpleNamespace(authenticator=FailingAuthenticator())
        ),
        state=SimpleNamespace(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await authenticate_request(request)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Authentication service unavailable"


@pytest.mark.asyncio
async def test_query_string_credentials_are_never_accepted():
    seen_keys: list[str] = []

    class CapturingAuthenticator:
        async def authenticate(self, api_key):
            seen_keys.append(api_key)
            return None

    app = SimpleNamespace(state=SimpleNamespace(authenticator=CapturingAuthenticator()))
    stream_request = SimpleNamespace(
        headers={},
        url=SimpleNamespace(path="/v1/stream"),
        query_params={"api_key": API_KEY},
        app=app,
        state=SimpleNamespace(),
    )
    flags_request = SimpleNamespace(
        headers={},
        url=SimpleNamespace(path="/v1/flags"),
        query_params={"api_key": API_KEY},
        app=app,
        state=SimpleNamespace(),
    )

    with pytest.raises(HTTPException) as stream_exc:
        await authenticate_request(stream_request)

    with pytest.raises(HTTPException) as exc_info:
        await authenticate_request(flags_request)

    assert stream_exc.value.status_code == 400
    assert exc_info.value.status_code == 400
    assert seen_keys == []


@pytest.mark.asyncio
async def test_stream_accepts_browser_credential_from_header():
    class BrowserAuthenticator:
        async def authenticate(self, api_key):
            return await PostgresAuthenticator(
                FakePool(browser_credential_row())
            ).authenticate(api_key)

    request = SimpleNamespace(
        headers={"x-api-key": BROWSER_KEY},
        url=SimpleNamespace(path="/v1/stream"),
        query_params={},
        app=SimpleNamespace(
            state=SimpleNamespace(authenticator=BrowserAuthenticator())
        ),
        state=SimpleNamespace(),
    )

    principal = await authenticate_request(request)
    assert principal.roles == frozenset({"events:write", "config:read"})


@pytest.mark.asyncio
async def test_authenticated_identity_endpoint_returns_canonical_principal():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/auth/me")

    assert response.status_code == 200
    assert response.json() == {
        "credential_id": "test-config",
        "project_id": "apdl",
        "roles": ["config:evaluate", "config:read", "config:write"],
    }


@pytest.mark.asyncio
async def test_authenticated_identity_endpoint_requires_api_key():
    class RejectingAuthenticator:
        async def authenticate(self, api_key):
            return None

    app.dependency_overrides.pop(authenticate_request, None)
    app.state.authenticator = RejectingAuthenticator()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"] == "Valid API key required"
