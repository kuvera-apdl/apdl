import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.auth import (
    PostgresAuthenticator,
    authenticate_request,
    delegated_auth_headers,
)


API_KEY = "proj_verifiedproject_0123456789abcdef0123456789abcdef"
CAPABILITY = "apdlcap_" + "A" * 43


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


def capability_row(**overrides):
    row = {
        "capability_id": "00000000-0000-0000-0000-000000000001",
        "token_hash": hashlib.sha256(CAPABILITY.encode("ascii")).hexdigest(),
        "project_id": "verifiedproject",
        "execution_kind": "agent_run",
        "execution_id": "run-1",
        "run_id": "run-1",
        "execution_owner_id": "lease-1",
        "roles": ["query:read"],
        "request_sha256": None,
        "consumed_at": None,
        "execution_active": True,
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
async def test_internal_capability_derives_query_authority_from_live_record():
    pool = FakePool(capability_row())
    request = SimpleNamespace(
        headers={"x-apdl-internal-capability": CAPABILITY},
        app=SimpleNamespace(
            state=SimpleNamespace(authenticator=PostgresAuthenticator(pool))
        ),
        state=SimpleNamespace(),
    )

    principal = await authenticate_request(request)

    assert principal is not None
    assert principal.project_id == "verifiedproject"
    assert principal.roles == frozenset({"query:read"})
    assert principal.auth_kind == "internal_capability"
    assert principal.capability_run_id == "run-1"
    assert principal.capability_execution_kind == "agent_run"
    assert request.state.principal is principal
    query, args = pool.connection.calls[0]
    assert "FROM agent_service_capabilities AS capability" in query
    assert "'query' = ANY(capability.audiences)" in query
    assert "capability.issued_at <= now()" in query
    assert "capability.expires_at > now()" in query
    assert "run.lease_expires_at > now()" in query
    assert args == (hashlib.sha256(CAPABILITY.encode("ascii")).hexdigest(),)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        None,  # Includes unknown and SQL-filtered expired capability records.
        capability_row(execution_active=False),
        capability_row(roles=["config:read"]),
        capability_row(roles=["query:read", "config:read"]),
    ],
)
async def test_internal_capability_rejects_expired_stale_or_non_query_authority(row):
    principal = await PostgresAuthenticator(FakePool(row)).authenticate_capability(
        CAPABILITY
    )

    assert principal is None


@pytest.mark.asyncio
async def test_query_read_capability_remains_reusable_for_delegation():
    pool = FakePool(capability_row())
    authenticator = PostgresAuthenticator(pool)

    first = await authenticator.authenticate_capability(CAPABILITY)
    second = await authenticator.authenticate_capability(CAPABILITY)

    assert first is not None
    assert second is not None
    assert len(pool.connection.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        capability_row(request_sha256="a" * 64),
        capability_row(consumed_at=datetime.now(timezone.utc)),
    ],
)
async def test_query_read_capability_rejects_mutation_binding_state(row):
    principal = await PostgresAuthenticator(FakePool(row)).authenticate_capability(
        CAPABILITY
    )

    assert principal is None


@pytest.mark.asyncio
async def test_authentication_rejects_dual_api_key_and_internal_capability():
    calls: list[tuple[str, str]] = []

    class CapturingAuthenticator:
        async def authenticate(self, value):
            calls.append(("api_key", value))
            return None

        async def authenticate_capability(self, value):
            calls.append(("capability", value))
            return None

    request = SimpleNamespace(
        headers={
            "x-api-key": API_KEY,
            "x-apdl-internal-capability": CAPABILITY,
        },
        app=SimpleNamespace(
            state=SimpleNamespace(authenticator=CapturingAuthenticator())
        ),
        state=SimpleNamespace(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await authenticate_request(request)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Multiple authentication mechanisms are not allowed"
    assert calls == []


def test_delegated_auth_forwards_exact_verified_capability():
    request = SimpleNamespace(
        headers={"x-apdl-internal-capability": CAPABILITY},
        state=SimpleNamespace(
            principal=SimpleNamespace(auth_kind="internal_capability")
        ),
    )

    assert delegated_auth_headers(request) == {
        "X-APDL-Internal-Capability": CAPABILITY
    }


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
