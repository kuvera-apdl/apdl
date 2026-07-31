"""Codegen workload access contracts for vault-owned credentials."""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest

from app.store.llm_credentials import (
    PROVIDERS,
    CredentialConfigurationError,
    CredentialNotFoundError,
    CredentialStoreError,
    ProjectCredentialStore,
    canonical_provider,
    validate_scope,
)


CREDENTIAL_ID = UUID("10000000-0000-4000-8000-000000000001")
CONNECTION_ID = UUID("20000000-0000-4000-8000-000000000002")


@pytest.mark.asyncio
async def test_requests_one_exact_codegen_credential_at_egress() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer codegen-token"
        assert json.loads(request.content) == {
            "schema_version": "llm_credential_access_request@1",
            "project_id": "Project42",
            "provider": "anthropic",
            "consumer": "codegen",
            "execution_id": "attempt-1",
            "purpose": "codegen.edit",
            "expected_credential_id": str(CREDENTIAL_ID),
            "expected_credential_version": 2,
        }
        return httpx.Response(
            200,
            json={
                "schema_version": "llm_credential_access@1",
                "access_id": "30000000-0000-4000-8000-000000000003",
                "connection_id": str(CONNECTION_ID),
                "credential_id": str(CREDENTIAL_ID),
                "credential_version": 2,
                "provider": "anthropic",
                "api_key": "provider-secret",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = ProjectCredentialStore(
            client,
            base_url="http://vault.test",
            token="codegen-token",
        )
        value = await store.load_active(
            "Project42",
            "anthropic",
            credential_id=CREDENTIAL_ID,
            credential_version=2,
            execution_id="attempt-1",
            purpose="codegen.edit",
        )
    assert value.api_key == "provider-secret"
    assert "provider-secret" not in repr(value)


@pytest.mark.asyncio
async def test_not_found_and_mismatched_response_fail_closed() -> None:
    async def missing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(missing)) as client:
        store = ProjectCredentialStore(
            client,
            base_url="http://vault.test",
            token="codegen-token",
        )
        with pytest.raises(CredentialNotFoundError):
            await store.load_active(
                "Project42",
                "anthropic",
                credential_id=CREDENTIAL_ID,
                credential_version=2,
                execution_id="attempt-1",
                purpose="codegen.edit",
            )

    async def mismatched(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": "llm_credential_access@1",
                "access_id": "30000000-0000-4000-8000-000000000003",
                "connection_id": str(CONNECTION_ID),
                "credential_id": str(CREDENTIAL_ID),
                "credential_version": 2,
                "provider": "openai",
                "api_key": "never-render-this",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(mismatched)) as client:
        store = ProjectCredentialStore(
            client,
            base_url="http://vault.test",
            token="codegen-token",
        )
        with pytest.raises(CredentialStoreError) as captured:
            await store.load_active(
                "Project42",
                "anthropic",
                credential_id=CREDENTIAL_ID,
                credential_version=2,
                execution_id="attempt-1",
                purpose="codegen.edit",
            )
    assert "never-render-this" not in str(captured.value)


def test_provider_and_environment_configuration_are_strict(monkeypatch) -> None:
    assert PROVIDERS == frozenset({"anthropic", "openai", "google", "xai"})
    assert canonical_provider("xai") == "xai"
    assert validate_scope("A1" * 32, "google") == "google"
    with pytest.raises(ValueError):
        canonical_provider("gemini")
    with pytest.raises(ValueError):
        validate_scope("project-with-hyphen", "openai")

    monkeypatch.setenv("LLM_VAULT_URL", "http://vault.test")
    monkeypatch.setenv("LLM_VAULT_CODEGEN_TOKEN", "short")
    with pytest.raises(CredentialConfigurationError):
        ProjectCredentialStore.from_environment()
