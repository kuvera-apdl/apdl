import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.auth import PostgresAuthenticator, authenticate_request


API_KEY = "proj_verifiedproject_0123456789abcdef0123456789abcdef"
BROWSER_KEY = "client_verifiedproject_0123456789abcdef0123456789abcdef"


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
    class CapturingAuthenticator:
        def __init__(self):
            self.seen_keys = []

        async def authenticate(self, api_key):
            self.seen_keys.append(api_key)
            return None

    authenticator = CapturingAuthenticator()
    request = SimpleNamespace(
        headers={},
        query_params={"api_key": BROWSER_KEY},
        app=SimpleNamespace(state=SimpleNamespace(authenticator=authenticator)),
        state=SimpleNamespace(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await authenticate_request(request)

    assert exc_info.value.status_code == 400
    assert authenticator.seen_keys == []
