"""Read-only Agents projection of vault-managed project LLM connections."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from app.llm.provider_catalog import ProviderModel
from app.main import app
from app.store.llm_connections import ConnectionMetadata


def _model(provider: str = "openai") -> ProviderModel:
    return ProviderModel(
        schema_version="llm_provider_model@1",
        provider=provider,
        model_id="gpt-5.4-mini",
        display_name="GPT-5.4 Mini",
        supported_tiers=("fast", "reasoning"),
        catalog_version="llm-provider-catalog@2",
        data_residency="global",
        allowed_data_classifications=("public", "internal", "confidential"),
        endpoint_host="api.openai.com",
        input_cost_per_million_tokens_usd_micros=250_000,
        output_cost_per_million_tokens_usd_micros=1_000_000,
        pricing_status="catalog_reviewed",
    )


def _connection() -> ConnectionMetadata:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    return ConnectionMetadata(
        project_id="demo",
        provider="openai",
        version=1,
        inventory_version=1,
        state="active",
        credential_id=uuid4(),
        catalog_version="llm-provider-catalog@2",
        validated_at=now,
        created_at=now,
        updated_at=now,
        revoked_at=None,
        model_count=1,
    )


class FakeConnectionStore:
    def __init__(self) -> None:
        self.connection = _connection()
        self.inventory = (_model(),)
        self.listed_projects: list[str] = []
        self.model_requests: list[tuple[str, str]] = []

    async def list(self, project_id: str) -> tuple[ConnectionMetadata, ...]:
        self.listed_projects.append(project_id)
        return (self.connection,)

    async def get_active_with_models(
        self,
        project_id: str,
        provider: str,
    ) -> tuple[ConnectionMetadata, tuple[ProviderModel, ...]]:
        self.model_requests.append((project_id, provider))
        return self.connection, self.inventory


@pytest.fixture
def connection_store() -> FakeConnectionStore:
    store = FakeConnectionStore()
    app.state.llm_connection_store = store
    return store


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
    assert "api_key" not in listed.text
    assert models.status_code == 200, models.text
    assert models.json()["connection_version"] == 1
    assert models.json()["models"][0]["model_id"] == "gpt-5.4-mini"
    assert connection_store.listed_projects == ["demo"]
    assert connection_store.model_requests == [("demo", "openai")]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["OpenAI", "unknown", "gpt"])
async def test_model_inventory_rejects_noncanonical_provider_paths(
    connection_store: FakeConnectionStore,
    provider: str,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/v1/agents/llm-connections/{provider}/models",
            params={"project_id": "demo"},
            headers={"X-API-Key": "ignored-by-test-auth"},
        )

    assert response.status_code == 422
    assert connection_store.model_requests == []


@pytest.mark.asyncio
async def test_legacy_mutation_routes_are_not_exposed(
    connection_store: FakeConnectionStore,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        replace = await client.put(
            "/v1/agents/llm-connections/openai",
            json={"project_id": "demo", "api_key": "must-not-be-accepted"},
        )
        revoke = await client.post(
            "/v1/agents/llm-connections/openai/revoke",
            json={"project_id": "demo", "version": 1, "reason": "obsolete"},
        )

    assert replace.status_code == 404
    assert revoke.status_code == 404
    assert connection_store.model_requests == []
