"""Strict HTTP contracts for owner-controlled Agents project setup."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest
from fastapi import Request

from app.auth import Principal, authenticate_request
from app.main import app
from app.store.llm_setup import (
    AgentsProjectSetup,
    AgentsSetupConflictError,
    ModelSelection,
    SetupAssignment,
    SetupConnection,
)


ACTOR_ID = UUID("20000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def _setup(*, state: str = "active", version: int = 2) -> AgentsProjectSetup:
    assignments = tuple(
        SetupAssignment(
            tier=tier,
            provider="openai",
            model="gpt-5.4-mini",
            connection_version=3,
            inventory_version=2,
            model_catalog_version="llm-provider-catalog@2",
            display_name="GPT-5.4 Mini",
            endpoint_url="https://api.openai.com/v1",
            endpoint_host="api.openai.com",
            data_residency="global",
            allowed_data_classifications=(
                "public",
                "internal",
                "confidential",
            ),
            input_cost_per_million_tokens_usd_micros=250_000,
            output_cost_per_million_tokens_usd_micros=1_000_000,
            current=True,
            assigned_at=NOW,
            updated_at=NOW,
        )
        for tier in ("fast", "reasoning")
    )
    return AgentsProjectSetup(
        project_id="demo",
        state=state,
        version=version,
        management_authority="owner",
        can_manage=True,
        assignments=assignments,
        connections=(
            SetupConnection(
                provider="openai",
                connection_version=3,
                inventory_version=2,
                state="active",
                catalog_version="llm-provider-catalog@2",
                current=True,
                validated_at=NOW,
            ),
        ),
        blockers=() if state == "active" else ("project_inactive",),
        analysis_ready=state == "active",
        required_data_residency="global",
        allow_cross_vendor_retry=False,
        project_daily_cost_limit_usd_micros=20_000_000,
        run_cost_limit_usd_micros=2_000_000,
        effectful_execution_authorized=False,
        effectful_execution_authorization_source=None,
        activated_at=NOW,
        deactivated_at=NOW if state == "inactive" else None,
        deactivation_reason="Paused by owner" if state == "inactive" else None,
    )


class _SetupStore:
    def __init__(self) -> None:
        self.get_args: tuple[object, ...] | None = None
        self.put_args: tuple[object, ...] | None = None
        self.deactivate_args: tuple[object, ...] | None = None
        self.error: Exception | None = None

    async def get(
        self,
        project_id: str,
        *,
        actor_user_id: UUID | None,
    ) -> AgentsProjectSetup:
        self.get_args = (project_id, actor_user_id)
        if self.error is not None:
            raise self.error
        return _setup()

    async def put(
        self,
        project_id: str,
        *,
        fast_model: ModelSelection,
        reasoning_model: ModelSelection,
        expected_version: int,
        actor_user_id: UUID,
    ) -> AgentsProjectSetup:
        self.put_args = (
            project_id,
            fast_model,
            reasoning_model,
            expected_version,
            actor_user_id,
        )
        if self.error is not None:
            raise self.error
        return _setup()

    async def deactivate(
        self,
        project_id: str,
        *,
        expected_version: int,
        actor_user_id: UUID,
        reason: str,
    ) -> AgentsProjectSetup:
        self.deactivate_args = (
            project_id,
            expected_version,
            actor_user_id,
            reason,
        )
        if self.error is not None:
            raise self.error
        return _setup(state="inactive", version=expected_version + 1)


async def _human_auth(request: Request) -> Principal:
    principal = Principal(
        credential_id="human-bound",
        project_id="demo",
        roles=frozenset({"agents:read"}),
        self_registered_project=True,
        execution_authorized=False,
        actor_user_id=str(ACTOR_ID),
    )
    request.state.principal = principal
    return principal


@pytest.fixture
def setup_store() -> _SetupStore:
    store = _SetupStore()
    app.state.agents_setup_store = store
    app.dependency_overrides[authenticate_request] = _human_auth
    return store


def _selection() -> dict[str, object]:
    return {
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "connection_version": 3,
        "inventory_version": 2,
    }


@pytest.mark.asyncio
async def test_get_returns_non_secret_setup_and_separate_effect_authority(
    setup_store: _SetupStore,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/agents/setup?project_id=demo")

    assert response.status_code == 200, response.text
    assert setup_store.get_args == ("demo", ACTOR_ID)
    body = response.json()
    assert body["schema_version"] == "agents_project_setup@1"
    assert body["state"] == "active"
    assert body["analysis_ready"] is True
    assert body["caller_capabilities"]["management_authority"] == "owner"
    assert body["effectful_execution"] == {
        "authorized": False,
        "authorization_source": None,
    }
    assert [assignment["tier"] for assignment in body["assignments"]] == [
        "fast",
        "reasoning",
    ]
    assert "api_key" not in response.text
    assert "credential_id" not in response.text


@pytest.mark.asyncio
async def test_put_forwards_only_exact_versioned_model_selections(
    setup_store: _SetupStore,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            "/v1/agents/setup",
            json={
                "project_id": "demo",
                "fast_model": _selection(),
                "reasoning_model": _selection(),
                "version": 1,
            },
        )

    assert response.status_code == 200, response.text
    assert setup_store.put_args == (
        "demo",
        ModelSelection(
            provider="openai",
            model="gpt-5.4-mini",
            connection_version=3,
            inventory_version=2,
        ),
        ModelSelection(
            provider="openai",
            model="gpt-5.4-mini",
            connection_version=3,
            inventory_version=2,
        ),
        1,
        ACTOR_ID,
    )


@pytest.mark.asyncio
async def test_mutation_defers_to_live_owner_or_delegate_without_read_role(
    setup_store: _SetupStore,
) -> None:
    async def human_without_read(request: Request) -> Principal:
        principal = Principal(
            credential_id="human-bound",
            project_id="demo",
            roles=frozenset(),
            self_registered_project=True,
            execution_authorized=False,
            actor_user_id=str(ACTOR_ID),
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = human_without_read
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            "/v1/agents/setup",
            json={
                "project_id": "demo",
                "fast_model": _selection(),
                "reasoning_model": _selection(),
                "version": 1,
            },
        )

    assert response.status_code == 200, response.text
    assert setup_store.put_args is not None


@pytest.mark.asyncio
async def test_put_rejects_unknown_top_level_and_nested_fields(
    setup_store: _SetupStore,
) -> None:
    fast_model = _selection()
    fast_model["endpoint_url"] = "https://attacker.invalid"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            "/v1/agents/setup",
            json={
                "project_id": "demo",
                "fast_model": fast_model,
                "reasoning_model": _selection(),
                "version": 1,
                "budget": 1,
            },
        )

    assert response.status_code == 422
    locations = {tuple(error["loc"]) for error in response.json()["detail"]}
    assert ("body", "fast_model", "endpoint_url") in locations
    assert ("body", "budget") in locations
    assert setup_store.put_args is None


@pytest.mark.asyncio
async def test_deactivate_requires_canonical_reason_and_maps_conflict(
    setup_store: _SetupStore,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        invalid = await client.post(
            "/v1/agents/setup/deactivate",
            json={"project_id": "demo", "version": 2, "reason": " padded "},
        )
        setup_store.error = AgentsSetupConflictError(
            "The Agents setup version or state changed"
        )
        conflict = await client.post(
            "/v1/agents/setup/deactivate",
            json={"project_id": "demo", "version": 2, "reason": "Paused by owner"},
        )

    assert invalid.status_code == 422
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == (
        "The Agents setup version or state changed"
    )
    assert setup_store.deactivate_args == (
        "demo",
        2,
        ACTOR_ID,
        "Paused by owner",
    )


@pytest.mark.asyncio
async def test_setup_mutation_requires_current_human_actor(
    setup_store: _SetupStore,
) -> None:
    async def service_auth(request: Request) -> Principal:
        principal = Principal(
            credential_id="service",
            project_id="demo",
            roles=frozenset({"agents:read"}),
            self_registered_project=False,
            execution_authorized=True,
            actor_user_id=None,
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = service_auth
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            "/v1/agents/setup",
            json={
                "project_id": "demo",
                "fast_model": _selection(),
                "reasoning_model": _selection(),
                "version": 1,
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "A current human session is required"
    assert setup_store.put_args is None
