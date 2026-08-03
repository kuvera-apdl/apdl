import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app import auth
from app.auth import (
    PostgresAuthenticator,
    Principal,
    authenticate_request,
    credential_has_current_role,
)
from app.main import app


API_KEY = "proj_verifiedproject_0123456789abcdef0123456789abcdef"
BROWSER_KEY = "client_verifiedproject_0123456789abcdef0123456789abcdef"
CAPABILITY = "apdlcap_" + "B" * 43


class FakeConnection:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.row

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        if "apdl_consume_agent_service_capability" in query:
            if self.row is None or self.row.get("consumed_at") is not None:
                return False
            self.row["consumed_at"] = datetime.now(timezone.utc)
            return True
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


def capability_row(**overrides):
    row = {
        "capability_id": "00000000-0000-0000-0000-000000000002",
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
async def test_internal_capability_accepts_live_config_audience_query_delegation():
    pool = FakePool(capability_row())
    request = SimpleNamespace(
        headers={"x-apdl-internal-capability": CAPABILITY},
        query_params={},
        url=SimpleNamespace(path="/v1/auth/me"),
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
    assert "'config' = ANY(capability.audiences)" in query
    assert "capability.expires_at > now()" in query
    assert "capability.issued_at <= now()" in query
    assert "run.lease_expires_at > now()" in query
    assert args == (hashlib.sha256(CAPABILITY.encode("ascii")).hexdigest(),)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        None,  # Includes unknown and SQL-filtered expired capability records.
        capability_row(execution_active=False),
        capability_row(roles=["agents:manage"]),
        capability_row(roles=["config:read", "config:write"]),
        capability_row(roles=["query:read", "config:read"]),
    ],
)
async def test_internal_capability_rejects_expired_stale_or_ambiguous_roles(row):
    principal = await PostgresAuthenticator(FakePool(row)).authenticate_capability(
        CAPABILITY
    )

    assert principal is None


class _CapabilityRequest:
    def __init__(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        idempotency_key: str | None = None,
    ) -> None:
        self.method = method
        self.scope = {"raw_path": path.encode("ascii")}
        self.url = SimpleNamespace(path=path)
        self.headers = (
            {"idempotency-key": idempotency_key}
            if idempotency_key is not None
            else {}
        )
        self._body = body

    async def body(self) -> bytes:
        return self._body


@pytest.mark.asyncio
async def test_config_mutation_capability_matches_request_and_is_consumed_once():
    payload = {"description": "approved", "key": "exp_checkout"}
    request_sha256 = auth._canonical_request_sha256(
        method="POST",
        path="/v1/admin/experiments",
        json_body=payload,
        idempotency_key="effect:1",
    )
    row = capability_row(
        execution_kind="approval_effect",
        execution_id="00000000-0000-0000-0000-000000000004",
        roles=["config:write"],
        request_sha256=request_sha256,
    )
    pool = FakePool(row)
    request = _CapabilityRequest(
        method="POST",
        path="/v1/admin/experiments",
        body=b'{"key":"exp_checkout","description":"approved"}',
        idempotency_key="effect:1",
    )

    first = await PostgresAuthenticator(pool).authenticate_capability(
        CAPABILITY,
        request,
    )
    replay = await PostgresAuthenticator(pool).authenticate_capability(
        CAPABILITY,
        request,
    )

    assert first is not None
    assert first.roles == frozenset({"config:write"})
    assert replay is None
    consume_calls = [
        call
        for call in pool.connection.calls
        if "apdl_consume_agent_service_capability" in call[0]
    ]
    assert len(consume_calls) == 2
    assert consume_calls[0][1][2] == request_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body", "idempotency_key"),
    [
        ("PUT", "/v1/admin/experiments", b'{"key":"exp_checkout"}', "effect:1"),
        ("POST", "/v1/admin/flags", b'{"key":"exp_checkout"}', "effect:1"),
        ("POST", "/v1/admin/experiments", b'{"key":"other"}', "effect:1"),
        ("POST", "/v1/admin/experiments", b'{"key":"exp_checkout"}', "effect:2"),
    ],
)
async def test_config_mutation_capability_rejects_request_binding_drift(
    method,
    path,
    body,
    idempotency_key,
):
    approved_body = {"key": "exp_checkout"}
    row = capability_row(
        execution_kind="approval_effect",
        roles=["config:write"],
        request_sha256=auth._canonical_request_sha256(
            method="POST",
            path="/v1/admin/experiments",
            json_body=approved_body,
            idempotency_key="effect:1",
        ),
    )
    pool = FakePool(row)

    principal = await PostgresAuthenticator(pool).authenticate_capability(
        CAPABILITY,
        _CapabilityRequest(
            method=method,
            path=path,
            body=body,
            idempotency_key=idempotency_key,
        ),
    )

    assert principal is None
    assert row["consumed_at"] is None
    assert not any(
        "apdl_consume_agent_service_capability" in query
        for query, _args in pool.connection.calls
    )


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
        query_params={},
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
    assert response.json()["detail"] == "Valid API key or internal capability required"
