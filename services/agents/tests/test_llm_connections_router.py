"""Strict, secret-free HTTP contracts for project LLM connections."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Request

from app.auth import Principal, authenticate_request
from app.llm.provider_catalog import ProviderModel
from app.main import app
from app.store.llm_connections import (
    ConnectionMetadata,
    LlmConnectionAuthorizationError,
)


ACTOR_ID = UUID("20000000-0000-4000-8000-000000000002")


def _model(provider: str = "openai") -> ProviderModel:
    return ProviderModel(
        schema_version="llm_provider_model@1",
        provider=provider,
        model_id="gpt-5.4-mini",
        display_name="GPT-5.4 Mini",
        supported_tiers=("fast", "reasoning"),
        catalog_version="llm-provider-catalog@2",
        data_residency="global",
        allowed_data_classifications=(
            "public",
            "internal",
            "confidential",
        ),
        endpoint_host="api.openai.com",
        input_cost_per_million_tokens_usd_micros=250_000,
        output_cost_per_million_tokens_usd_micros=1_000_000,
        pricing_status="catalog_reviewed",
    )


def _connection(
    *,
    version: int = 1,
    state: str = "active",
    model_count: int = 1,
) -> ConnectionMetadata:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    return ConnectionMetadata(
        project_id="demo",
        provider="openai",
        version=version,
        inventory_version=1,
        state=state,
        credential_id=uuid4(),
        catalog_version="llm-provider-catalog@2",
        validated_at=now,
        created_at=now,
        updated_at=now,
        revoked_at=now if state == "revoked" else None,
        model_count=model_count,
    )


class FakeConnectionStore:
    def __init__(self) -> None:
        self.authorized = True
        self.put_args: tuple[object, ...] | None = None
        self.connection = _connection()
        self.inventory = (_model(),)

    async def assert_mutation_authority(
        self,
        project_id: str,
        actor_user_id: UUID,
    ) -> None:
        if not self.authorized:
            raise LlmConnectionAuthorizationError("Live authority was revoked")

    async def put(
        self,
        project_id: str,
        provider: str,
        api_key: str,
        models: tuple[ProviderModel, ...],
        *,
        expected_version: int,
        actor_user_id: UUID,
    ) -> ConnectionMetadata:
        self.put_args = (
            project_id,
            provider,
            api_key,
            models,
            expected_version,
            actor_user_id,
        )
        return self.connection

    async def list(self, project_id: str) -> tuple[ConnectionMetadata, ...]:
        return (self.connection,)

    async def get_active_with_models(
        self,
        project_id: str,
        provider: str,
    ) -> tuple[ConnectionMetadata, tuple[ProviderModel, ...]]:
        return self.connection, self.inventory


async def _human_auth(request: Request):
    principal = Principal(
        credential_id="human-bound",
        project_id="demo",
        roles=frozenset({"agents:read"}),
        self_registered_project=False,
        execution_authorized=False,
        actor_user_id=str(ACTOR_ID),
    )
    request.state.principal = principal
    return principal


@pytest.fixture
def connection_store() -> FakeConnectionStore:
    store = FakeConnectionStore()
    app.state.llm_connection_store = store
    app.dependency_overrides[authenticate_request] = _human_auth
    return store


@pytest.mark.asyncio
async def test_put_validates_and_never_echoes_api_key(
    connection_store: FakeConnectionStore,
    monkeypatch,
) -> None:
    secret = "provider-secret-sentinel"
    discovered = (_model(),)
    calls: list[tuple[str, str]] = []

    async def fake_discovery(provider: str, api_key: str):
        calls.append((provider, api_key))
        return discovered

    monkeypatch.setattr(
        "app.routers.llm_connections.discover_models",
        fake_discovery,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            "/v1/agents/llm-connections/openai",
            headers={"X-API-Key": "ignored-by-test-auth"},
            json={"project_id": "demo", "api_key": secret, "version": 0},
        )

    assert response.status_code == 200, response.text
    assert secret not in response.text
    assert "credential_id" not in response.text
    assert calls == [("openai", secret)]
    assert connection_store.put_args == (
        "demo",
        "openai",
        secret,
        discovered,
        0,
        ACTOR_ID,
    )
    assert response.json()["models"][0]["supported_tiers"] == [
        "fast",
        "reasoning",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/v1/agents/llm-connections/OpenAI",
        "/v1/agents/llm-connections/unknown",
    ],
)
async def test_provider_path_rejects_aliases_and_unknown_values(
    connection_store: FakeConnectionStore,
    path: str,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            path,
            headers={"X-API-Key": "ignored-by-test-auth"},
            json={"project_id": "demo", "api_key": "secret", "version": 0},
        )

    assert response.status_code == 422
    assert connection_store.put_args is None


@pytest.mark.asyncio
async def test_put_rejects_duplicate_provider_body_field(
    connection_store: FakeConnectionStore,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            "/v1/agents/llm-connections/openai",
            headers={"X-API-Key": "ignored-by-test-auth"},
            json={
                "project_id": "demo",
                "provider": "openai",
                "api_key": "secret",
                "version": 0,
            },
        )

    assert response.status_code == 422
    assert connection_store.put_args is None


@pytest.mark.asyncio
async def test_put_rejects_api_key_over_utf8_byte_limit(
    connection_store: FakeConnectionStore,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            "/v1/agents/llm-connections/openai",
            headers={"X-API-Key": "ignored-by-test-auth"},
            json={"project_id": "demo", "api_key": "é" * 9_000, "version": 0},
        )

    assert response.status_code == 422
    assert connection_store.put_args is None


@pytest.mark.asyncio
async def test_revoke_rejects_noncanonical_reason(
    connection_store: FakeConnectionStore,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/agents/llm-connections/openai/revoke",
            headers={"X-API-Key": "ignored-by-test-auth"},
            json={
                "project_id": "demo",
                "version": 1,
                "reason": "line one\nline two",
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_mutation_requires_live_human_authority(
    connection_store: FakeConnectionStore,
) -> None:
    connection_store.authorized = False
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(
            "/v1/agents/llm-connections/openai",
            headers={"X-API-Key": "ignored-by-test-auth"},
            json={"project_id": "demo", "api_key": "secret", "version": 0},
        )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Live authority was revoked"
    assert connection_store.put_args is None


@pytest.mark.asyncio
async def test_list_and_model_inventory_are_project_scoped_and_non_secret(
    connection_store: FakeConnectionStore,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        listed = await client.get(
            "/v1/agents/llm-connections",
            params={"project_id": "demo"},
            headers={"X-API-Key": "ignored-by-test-auth"},
        )
        models = await client.get(
            "/v1/agents/llm-connections/openai/models",
            params={"project_id": "demo"},
            headers={"X-API-Key": "ignored-by-test-auth"},
        )

    assert listed.status_code == 200, listed.text
    assert listed.json()["connections"][0]["provider"] == "openai"
    assert "credential_id" not in listed.text
    assert models.status_code == 200, models.text
    assert models.json()["connection_version"] == 1
    assert models.json()["models"][0]["model_id"] == "gpt-5.4-mini"
