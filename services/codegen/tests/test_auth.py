"""Project-scoped API-key authentication and authorization."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.auth import (
    Principal,
    PostgresAuthenticator,
    authenticate_request,
    require_role,
)
from app.main import app
from tests.fakes import FakePool

_VALID_KEY = "proj_demo_0123456789abcdef"


class _AuthConnection:
    def __init__(self, row: dict | None) -> None:
        self._row = row

    async def fetchrow(self, _query: str, provided_hash: str):
        if self._row is None or self._row["key_hash"] != provided_hash:
            return None
        return self._row


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
        "execution_authorized": execution_authorized,
    }


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
