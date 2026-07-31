"""Strict secret-free HTTP contracts for Codegen project LLM connections."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import Request

from app.auth import Principal, authenticate_request
from app.llm.provider_catalog import ProviderModel, catalog_model
from app.main import app
from app.store.llm_connections import (
    ConnectionMetadata,
    LlmConnectionAuthorizationError,
    LlmConnectionConflictError,
)
from app.store.llm_credentials import DecryptedCredential


ACTOR_ID = UUID("20000000-0000-4000-8000-000000000002")
SECRET = "codegen-provider-secret-sentinel"


def _model() -> ProviderModel:
    model = catalog_model("openai", "gpt-5.4-mini")
    assert model is not None
    return model


def _connection(
    *,
    version: int = 1,
    inventory_version: int = 1,
    state: str = "active",
) -> ConnectionMetadata:
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    return ConnectionMetadata(
        project_id="demo",
        provider="openai",
        version=version,
        inventory_version=inventory_version,
        state=state,
        credential_id=uuid4(),
        catalog_version="codegen-provider-catalog@1",
        validated_at=now,
        created_at=now,
        updated_at=now,
        revoked_at=now if state == "revoked" else None,
        model_count=1 if state == "active" else 0,
    )


class FakeConnectionStore:
    def __init__(self) -> None:
        self.authorized = True
        self.connection = _connection()
        self.models = (_model(),)
        self.put_args: tuple[object, ...] | None = None
        self.refresh_args: tuple[object, ...] | None = None
        self.revoke_args: tuple[object, ...] | None = None
        self.refresh_conflict = False

    async def assert_mutation_authority(
        self,
        project_id: str,
        actor_user_id: UUID,
    ) -> None:
        if not self.authorized:
            raise LlmConnectionAuthorizationError(
                "Live authority was revoked"
            )

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

    async def list(
        self, project_id: str
    ) -> tuple[ConnectionMetadata, ...]:
        return (self.connection,)

    async def get_active_with_models(
        self,
        project_id: str,
        provider: str,
    ) -> tuple[ConnectionMetadata, tuple[ProviderModel, ...]]:
        return self.connection, self.models

    async def credential_for_refresh(
        self,
        project_id: str,
        provider: str,
        *,
        expected_version: int,
    ) -> DecryptedCredential:
        if self.refresh_conflict:
            raise LlmConnectionConflictError(
                "The provider credential changed"
            )
        return DecryptedCredential(
            credential_id=self.connection.credential_id,
            project_id=project_id,
            provider="openai",
            credential_version=1,
            api_key=SECRET,
        )

    async def refresh(
        self,
        project_id: str,
        provider: str,
        models: tuple[ProviderModel, ...],
        *,
        expected_version: int,
        expected_credential_id: UUID,
        actor_user_id: UUID,
    ) -> ConnectionMetadata:
        self.refresh_args = (
            project_id,
            provider,
            models,
            expected_version,
            expected_credential_id,
            actor_user_id,
        )
        return self.connection

    async def revoke(
        self,
        project_id: str,
        provider: str,
        *,
        expected_version: int,
        actor_user_id: UUID,
    ) -> ConnectionMetadata:
        self.revoke_args = (
            project_id,
            provider,
            expected_version,
            actor_user_id,
        )
        return _connection(
            version=expected_version + 1,
            inventory_version=2,
            state="revoked",
        )


async def _human_auth(request: Request) -> Principal:
    principal = Principal(
        credential_id="human-bound",
        project_id="demo",
        roles=frozenset(
            {"agents:read", "agents:manage", "credentials:manage"}
        ),
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
    yield store
    app.dependency_overrides.pop(authenticate_request, None)


@pytest_asyncio.fixture
async def client(connection_store: FakeConnectionStore):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as value:
        yield value


@pytest.mark.asyncio
async def test_put_discovers_and_never_echoes_secret(
    client: httpx.AsyncClient,
    connection_store: FakeConnectionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    async def discover(provider: str, api_key: str):
        calls.append((provider, api_key))
        return (_model(),)

    monkeypatch.setattr(
        "app.routers.llm_connections.discover_models", discover
    )
    response = await client.put(
        "/v1/llm-connections/openai",
        json={"project_id": "demo", "api_key": SECRET, "version": 0},
    )

    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    assert "credential_id" not in response.text
    assert calls == [("openai", SECRET)]
    assert connection_store.put_args == (
        "demo",
        "openai",
        SECRET,
        (_model(),),
        0,
        ACTOR_ID,
    )
    assert response.json()["models"][0]["supported_roles"] == [
        "editor",
        "helper",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["OpenAI", "unknown", "gpt"])
async def test_provider_aliases_are_rejected_before_discovery(
    client: httpx.AsyncClient,
    connection_store: FakeConnectionStore,
    provider: str,
) -> None:
    response = await client.put(
        f"/v1/llm-connections/{provider}",
        json={"project_id": "demo", "api_key": SECRET, "version": 0},
    )

    assert response.status_code == 422
    assert SECRET not in response.text
    assert connection_store.put_args is None


@pytest.mark.asyncio
async def test_duplicate_json_key_is_rejected_without_reflection(
    client: httpx.AsyncClient,
    connection_store: FakeConnectionStore,
) -> None:
    body = (
        '{"project_id":"demo","api_key":"first",'
        f'"api_key":"{SECRET}","version":0}}'
    )
    response = await client.put(
        "/v1/llm-connections/openai",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert SECRET not in response.text
    assert connection_store.put_args is None


@pytest.mark.asyncio
async def test_validation_never_reflects_secret_or_unknown_fields(
    client: httpx.AsyncClient,
    connection_store: FakeConnectionStore,
) -> None:
    response = await client.put(
        "/v1/llm-connections/openai",
        json={
            "project_id": "demo",
            "api_key": {"secret": SECRET},
            "version": 0,
            "endpoint": "https://attacker.invalid",
        },
    )

    assert response.status_code == 422
    assert SECRET not in response.text
    assert "attacker.invalid" not in response.text
    assert connection_store.put_args is None


@pytest.mark.asyncio
async def test_list_and_models_are_non_secret(
    client: httpx.AsyncClient,
) -> None:
    listed = await client.get(
        "/v1/llm-connections", params={"project_id": "demo"}
    )
    models = await client.get(
        "/v1/llm-connections/openai/models",
        params={"project_id": "demo"},
    )

    assert listed.status_code == 200
    assert models.status_code == 200
    for response in (listed, models):
        assert SECRET not in response.text
        assert "credential_id" not in response.text
        assert "ciphertext" not in response.text
        assert "litellm" not in response.text


@pytest.mark.asyncio
async def test_authority_loss_prevents_mutation_before_discovery(
    client: httpx.AsyncClient,
    connection_store: FakeConnectionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_store.authorized = False
    called = False

    async def discover(provider: str, api_key: str):
        nonlocal called
        called = True
        return (_model(),)

    monkeypatch.setattr(
        "app.routers.llm_connections.discover_models", discover
    )
    response = await client.put(
        "/v1/llm-connections/openai",
        json={"project_id": "demo", "api_key": SECRET, "version": 0},
    )

    assert response.status_code == 403
    assert called is False
    assert SECRET not in response.text


@pytest.mark.asyncio
async def test_refresh_race_is_an_optimistic_conflict(
    client: httpx.AsyncClient,
    connection_store: FakeConnectionStore,
) -> None:
    connection_store.refresh_conflict = True
    response = await client.post(
        "/v1/llm-connections/openai/refresh-models",
        json={"project_id": "demo", "version": 1},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "The provider credential changed"
    assert connection_store.refresh_args is None


@pytest.mark.asyncio
async def test_revoke_uses_exact_version_actor_and_reason(
    client: httpx.AsyncClient,
    connection_store: FakeConnectionStore,
) -> None:
    response = await client.post(
        "/v1/llm-connections/openai/revoke",
        json={
            "project_id": "demo",
            "version": 1,
            "reason": SECRET,
        },
    )

    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    assert response.json()["state"] == "revoked"
    assert connection_store.revoke_args == (
        "demo",
        "openai",
        1,
        ACTOR_ID,
    )


@pytest.mark.asyncio
async def test_cross_project_read_is_forbidden_without_store_call(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/v1/llm-connections", params={"project_id": "other"}
    )
    assert response.status_code == 403
