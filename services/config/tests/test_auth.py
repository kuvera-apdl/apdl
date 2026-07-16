import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.auth import PostgresAuthenticator, authenticate_request
from app.main import app


API_KEY = "proj_verifiedproject_0123456789abcdef0123456789abcdef"


class FakeConnection:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, query, *args):
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


def credential_row(**overrides):
    row = {
        "credential_id": "credential-1",
        "project_id": "verifiedproject",
        "key_hash": hashlib.sha256(API_KEY.encode()).hexdigest(),
        "roles": ["query:read", "events:write"],
        "active": True,
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }
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
async def test_authentication_rejects_misprovisioned_project_hint():
    principal = await PostgresAuthenticator(
        FakePool(credential_row(project_id="otherproject"))
    ).authenticate(API_KEY)
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
async def test_authentication_dependency_fails_closed_when_registry_is_unavailable():
    class FailingAuthenticator:
        async def authenticate(self, api_key):
            raise ConnectionError("database unavailable")

    request = SimpleNamespace(
        headers={"x-api-key": API_KEY},
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
async def test_stream_accepts_the_legacy_query_credential_only_on_stream_path():
    seen_keys: list[str] = []

    class CapturingAuthenticator:
        async def authenticate(self, api_key):
            seen_keys.append(api_key)
            if api_key != API_KEY:
                return None
            return await PostgresAuthenticator(FakePool(credential_row())).authenticate(
                api_key
            )

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

    principal = await authenticate_request(stream_request)
    assert principal.project_id == "verifiedproject"

    with pytest.raises(HTTPException) as exc_info:
        await authenticate_request(flags_request)

    assert exc_info.value.status_code == 401
    assert seen_keys == [API_KEY, ""]


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
