"""Agents workload access contracts for vault-owned credentials."""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest

from app.store.llm_credentials import (
    CredentialConfigurationError,
    CredentialNotFoundError,
    CredentialStoreError,
    ProjectCredentialStore,
)


CREDENTIAL_ID = UUID("10000000-0000-4000-8000-000000000001")
CONNECTION_ID = UUID("20000000-0000-4000-8000-000000000002")


@pytest.mark.asyncio
async def test_requests_one_exact_agents_credential_at_egress() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer agents-token"
        assert json.loads(request.content) == {
            "schema_version": "llm_credential_access_request@1",
            "project_id": "demo",
            "provider": "openai",
            "consumer": "agents",
            "execution_id": "attempt-1",
            "purpose": "agent.experiment.reason",
            "expected_credential_id": str(CREDENTIAL_ID),
            "expected_credential_version": 3,
        }
        return httpx.Response(
            200,
            json={
                "schema_version": "llm_credential_access@1",
                "access_id": "30000000-0000-4000-8000-000000000003",
                "connection_id": str(CONNECTION_ID),
                "credential_id": str(CREDENTIAL_ID),
                "credential_version": 3,
                "provider": "openai",
                "api_key": "provider-secret",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = ProjectCredentialStore(
            client,
            base_url="http://vault.test",
            token="agents-token",
        )
        value = await store.load_active(
            "demo",
            "openai",
            credential_id=CREDENTIAL_ID,
            credential_version=3,
            execution_id="attempt-1",
            purpose="agent.experiment.reason",
        )
    assert value.api_key == "provider-secret"
    assert "provider-secret" not in repr(value)


@pytest.mark.asyncio
async def test_missing_or_mismatched_authority_fails_closed() -> None:
    async def missing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(missing)) as client:
        store = ProjectCredentialStore(
            client,
            base_url="http://vault.test",
            token="agents-token",
        )
        with pytest.raises(CredentialNotFoundError):
            await store.load_active(
                "demo",
                "openai",
                credential_id=CREDENTIAL_ID,
                credential_version=3,
                execution_id="attempt-1",
                purpose="agent.reason",
            )

    async def mismatched(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": "llm_credential_access@1",
                "access_id": "30000000-0000-4000-8000-000000000003",
                "connection_id": str(CONNECTION_ID),
                "credential_id": str(CREDENTIAL_ID),
                "credential_version": 4,
                "provider": "openai",
                "api_key": "never-render-this",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(mismatched)) as client:
        store = ProjectCredentialStore(
            client,
            base_url="http://vault.test",
            token="agents-token",
        )
        with pytest.raises(CredentialStoreError) as captured:
            await store.load_active(
                "demo",
                "openai",
                credential_id=CREDENTIAL_ID,
                credential_version=3,
                execution_id="attempt-1",
                purpose="agent.reason",
            )
    assert "never-render-this" not in str(captured.value)


def test_environment_requires_absolute_url_and_scoped_token(monkeypatch) -> None:
    monkeypatch.setenv("LLM_VAULT_URL", "not-a-url")
    monkeypatch.setenv("LLM_VAULT_AGENTS_TOKEN", "x" * 32)
    with pytest.raises(CredentialConfigurationError):
        ProjectCredentialStore.from_environment()

    monkeypatch.setenv("LLM_VAULT_URL", "http://vault.test")
    monkeypatch.setenv("LLM_VAULT_AGENTS_TOKEN", "short")
    with pytest.raises(CredentialConfigurationError):
        ProjectCredentialStore.from_environment()
