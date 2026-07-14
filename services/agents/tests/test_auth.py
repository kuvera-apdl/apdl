import hashlib
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.auth import (
    Principal,
    PostgresAuthenticator,
    authenticate_request,
    require_role,
)


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
        "self_registered_project": False,
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
    assert principal.self_registered_project is False
    query, args = pool.connection.calls[0]
    assert "JOIN admin_projects AS project" in query
    assert "project.created_by IS NOT NULL" in query
    assert "WHERE credential.key_hash = $1" in query
    assert args == (hashlib.sha256(API_KEY.encode()).hexdigest(),)


@pytest.mark.asyncio
async def test_authentication_marks_self_registered_project_from_project_row():
    pool = FakePool(credential_row(self_registered_project=True))

    principal = await PostgresAuthenticator(pool).authenticate(API_KEY)

    assert principal is not None
    assert principal.self_registered_project is True
    with pytest.raises(FrozenInstanceError):
        principal.self_registered_project = False


def _request_for(principal: Principal):
    return SimpleNamespace(state=SimpleNamespace(principal=principal))


@pytest.mark.parametrize("role", ["agents:run", "agents:manage", "agents:approve"])
def test_self_registered_project_cannot_use_agent_execution_roles(role):
    principal = Principal(
        credential_id="overprivileged",
        project_id="verifiedproject",
        roles=frozenset(
            {"agents:read", "agents:run", "agents:manage", "agents:approve"}
        ),
        self_registered_project=True,
    )

    with pytest.raises(HTTPException) as exc_info:
        require_role(_request_for(principal), role)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == (
        "Agents execution is unavailable for self-registered projects"
    )


def test_self_registered_project_keeps_read_access():
    principal = Principal(
        credential_id="reader",
        project_id="verifiedproject",
        roles=frozenset({"agents:read"}),
        self_registered_project=True,
    )

    assert require_role(_request_for(principal), "agents:read") is principal


def test_operator_project_keeps_agent_execution_roles():
    principal = Principal(
        credential_id="operator",
        project_id="verifiedproject",
        roles=frozenset({"agents:run", "agents:manage", "agents:approve"}),
        self_registered_project=False,
    )

    assert require_role(_request_for(principal), "agents:run") is principal


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
