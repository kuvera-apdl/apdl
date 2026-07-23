"""Authenticated, project-scoped execution-capability contracts."""

from __future__ import annotations

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.auth import Principal, authenticate_request
from app.main import app
from app.routers import capabilities


@pytest.mark.asyncio
async def test_execution_capability_exposes_operator_policy_and_codegen_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTS_ENABLE_AUTONOMOUS_MUTATIONS", "true")

    async def available(project_id: str) -> str:
        assert project_id == "demo"
        return "available"

    monkeypatch.setattr(capabilities, "codegen_changeset_capability", available)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/agents/capabilities/execution",
            params={"project_id": "demo"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "agents_project_execution_capabilities@1",
        "project_id": "demo",
        "autonomous_mutations_operator_enabled": True,
        "codegen_changeset_creation": "available",
    }


@pytest.mark.asyncio
async def test_execution_capability_is_approval_only_and_codegen_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENTS_ENABLE_AUTONOMOUS_MUTATIONS", raising=False)

    async def unavailable(_project_id: str) -> str:
        return "unavailable"

    monkeypatch.setattr(capabilities, "codegen_changeset_capability", unavailable)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/agents/capabilities/execution",
            params={"project_id": "demo"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "agents_project_execution_capabilities@1",
        "project_id": "demo",
        "autonomous_mutations_operator_enabled": False,
        "codegen_changeset_creation": "unavailable",
    }


@pytest.mark.asyncio
async def test_execution_capability_rejects_cross_project_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def must_not_probe(_project_id: str) -> str:
        raise AssertionError("cross-project requests must fail before service egress")

    monkeypatch.setattr(capabilities, "codegen_changeset_capability", must_not_probe)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/agents/capabilities/execution",
            params={"project_id": "other"},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_execution_capability_requires_project_execution_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def read_only(request: Request) -> Principal:
        principal = Principal(
            credential_id="read-only",
            project_id="demo",
            roles=frozenset({"agents:read"}),
            self_registered_project=False,
            execution_authorized=True,
        )
        request.state.principal = principal
        return principal

    async def must_not_probe(_project_id: str) -> str:
        raise AssertionError("read-only requests must fail before service egress")

    app.dependency_overrides[authenticate_request] = read_only
    monkeypatch.setattr(capabilities, "codegen_changeset_capability", must_not_probe)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/agents/capabilities/execution",
            params={"project_id": "demo"},
        )

    assert response.status_code == 403
