"""Project-scoped API-key authentication and authorization."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app import auth
from app.auth import (
    Principal,
    PostgresAuthenticator,
    authenticate_request,
    require_role,
)
from app.main import app
from tests.fakes import FakePool

_VALID_KEY = "proj_demo_0123456789abcdef"
_CAPABILITY = "apdlcap_" + "C" * 43


class _AuthConnection:
    def __init__(self, row: dict | None) -> None:
        self._row = row
        self.last_query = ""

    async def fetchrow(self, query: str, provided_hash: str):
        self.last_query = query
        hash_field = (
            "token_hash"
            if "FROM agent_service_capabilities AS capability" in query
            else "key_hash"
        )
        if self._row is None or self._row.get(hash_field) != provided_hash:
            return None
        return self._row

    async def fetchval(self, query: str, *args):
        self.last_query = query
        if "apdl_consume_agent_service_capability" not in query:
            return None
        if self._row is None or self._row.get("consumed_at") is not None:
            return False
        self._row["consumed_at"] = datetime.now(timezone.utc)
        return True


class _AuthPool:
    def __init__(self, row: dict | None) -> None:
        self._connection = _AuthConnection(row)

    @asynccontextmanager
    async def acquire(self):
        yield self._connection


def _row(
    key: str = _VALID_KEY,
    *,
    project_id: str = "demo",
    roles: tuple[str, ...] = ("agents:read", "agents:manage"),
    active: bool = True,
    expires_at: datetime | None = None,
    execution_authorized: bool = True,
) -> dict:
    return {
        "credential_id": "codegen-test",
        "project_id": project_id,
        "key_hash": hashlib.sha256(key.encode()).hexdigest(),
        "roles": roles,
        "active": active,
        "expires_at": expires_at,
        "actor_user_id": None,
        "execution_authorized": execution_authorized,
    }


def _capability_row(**overrides) -> dict:
    request_body = {"project_id": "demo", "run_id": "run-1"}
    row = {
        "capability_id": "00000000-0000-0000-0000-000000000003",
        "token_hash": hashlib.sha256(_CAPABILITY.encode("ascii")).hexdigest(),
        "project_id": "demo",
        "execution_kind": "approval_effect",
        "execution_id": "00000000-0000-0000-0000-000000000004",
        "run_id": "run-1",
        "execution_owner_id": "effect-worker-1",
        "roles": ["agents:manage"],
        "request_sha256": auth._canonical_request_sha256(
            method="POST",
            path="/v1/changesets",
            json_body=request_body,
            idempotency_key=None,
        ),
        "consumed_at": None,
        "execution_authorized": True,
        "execution_active": True,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_api_key_is_required_and_internal_token_has_no_authority(
    monkeypatch, authorized_codegen_request
):
    app.dependency_overrides.pop(authenticate_request, None)
    monkeypatch.setenv("APDL_INTERNAL_TOKEN", "obsolete-global-token")
    app.state.pg_pool = FakePool()
    app.state.authenticator = PostgresAuthenticator(_AuthPool(_row()))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = await client.get("/v1/connections/demo")
        obsolete = await client.get(
            "/v1/connections/demo",
            headers={"X-APDL-Internal-Token": "obsolete-global-token"},
        )
        valid = await client.get(
            "/v1/connections/demo", headers={"X-API-Key": _VALID_KEY}
        )

    assert missing.status_code == 401
    assert obsolete.status_code == 401
    assert valid.status_code == 404


def test_execution_role_requires_operator_project_authorization():
    principal = Principal(
        credential_id="codegen-test",
        project_id="demo",
        roles=frozenset({"agents:manage"}),
        execution_authorized=False,
    )
    request = SimpleNamespace(state=SimpleNamespace(principal=principal))

    with pytest.raises(HTTPException) as exc_info:
        require_role(request, "agents:manage")

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == (
        "Codegen execution requires operator project authorization"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row,key",
    [
        (None, _VALID_KEY),
        (_row(active=False), _VALID_KEY),
        (
            _row(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)),
            _VALID_KEY,
        ),
        (
            _row("proj_other_0123456789abcdef", project_id="demo"),
            "proj_other_0123456789abcdef",
        ),
    ],
)
async def test_invalid_revoked_expired_or_mismatched_keys_are_rejected(
    row, key, authorized_codegen_request
):
    app.dependency_overrides.pop(authenticate_request, None)
    app.state.pg_pool = FakePool()
    app.state.authenticator = PostgresAuthenticator(_AuthPool(row))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/connections/demo", headers={"X-API-Key": key}
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_internal_capability_derives_live_codegen_authority():
    pool = _AuthPool(_capability_row())

    async def body():
        return b'{"run_id":"run-1","project_id":"demo"}'

    request = SimpleNamespace(
        headers={"x-apdl-internal-capability": _CAPABILITY},
        method="POST",
        scope={"raw_path": b"/v1/changesets"},
        url=SimpleNamespace(path="/v1/changesets"),
        body=body,
        app=SimpleNamespace(
            state=SimpleNamespace(authenticator=PostgresAuthenticator(pool))
        ),
        state=SimpleNamespace(),
    )

    principal = await authenticate_request(request)

    assert principal is not None
    assert principal.project_id == "demo"
    assert principal.roles == frozenset({"agents:manage"})
    assert principal.execution_authorized is True
    assert principal.auth_kind == "internal_capability"
    assert principal.capability_run_id == "run-1"
    assert principal.capability_execution_kind == "approval_effect"
    assert request.state.principal is principal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        None,  # Includes unknown and SQL-filtered expired capability records.
        _capability_row(execution_active=False),
        _capability_row(roles=["agents:run"]),
        _capability_row(roles=["agents:read", "agents:manage"]),
    ],
)
async def test_internal_capability_rejects_expired_stale_or_ambiguous_roles(row):
    principal = await PostgresAuthenticator(
        _AuthPool(row)
    ).authenticate_capability(_CAPABILITY)

    assert principal is None


@pytest.mark.asyncio
async def test_codegen_capability_lookup_is_audience_and_expiry_scoped():
    connection = _AuthConnection(
        _capability_row(
            execution_kind="agent_run",
            execution_id="run-1",
            roles=["agents:read"],
            request_sha256=None,
        )
    )
    pool = _AuthPool(None)
    pool._connection = connection

    assert await PostgresAuthenticator(pool).authenticate_capability(_CAPABILITY)

    # Expiry and audience are database predicates so invalid records are never
    # materialized into a principal in application memory.
    # The fake records the exact production query for that contract assertion.
    query = connection.last_query
    assert "'codegen' = ANY(capability.audiences)" in query
    assert "capability.issued_at <= now()" in query
    assert "capability.expires_at > now()" in query
    assert "effect.lease_expires_at > now()" in query


@pytest.mark.asyncio
async def test_codegen_mutation_capability_is_consumed_once():
    row = _capability_row()
    pool = _AuthPool(row)

    async def body():
        return b'{"run_id":"run-1","project_id":"demo"}'

    request = SimpleNamespace(
        headers={},
        method="POST",
        scope={"raw_path": b"/v1/changesets"},
        url=SimpleNamespace(path="/v1/changesets"),
        body=body,
    )

    first = await PostgresAuthenticator(pool).authenticate_capability(
        _CAPABILITY,
        request,
    )
    replay = await PostgresAuthenticator(pool).authenticate_capability(
        _CAPABILITY,
        request,
    )

    assert first is not None
    assert replay is None


@pytest.mark.asyncio
async def test_codegen_mutation_capability_rejects_unapproved_path_without_consuming():
    row = _capability_row()
    pool = _AuthPool(row)

    async def body():
        return b'{"run_id":"run-1","project_id":"demo"}'

    request = SimpleNamespace(
        headers={},
        method="POST",
        scope={"raw_path": b"/v1/changesets/unrelated/revert"},
        url=SimpleNamespace(path="/v1/changesets/unrelated/revert"),
        body=body,
    )

    principal = await PostgresAuthenticator(pool).authenticate_capability(
        _CAPABILITY,
        request,
    )

    assert principal is None
    assert row["consumed_at"] is None


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
            "x-api-key": _VALID_KEY,
            "x-apdl-internal-capability": _CAPABILITY,
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
