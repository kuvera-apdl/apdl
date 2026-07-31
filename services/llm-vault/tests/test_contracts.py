from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.contracts import CreateConnectionRequest, CredentialAccessRequest


def test_create_contract_accepts_only_canonical_consumer_order() -> None:
    canonical = {
        "project_id": "demo",
        "provider": "openai",
        "label": "Production",
        "api_key": "secret",
        "consumers": ["agents", "codegen"],
    }
    parsed = CreateConnectionRequest.model_validate_json(json.dumps(canonical))
    assert parsed.consumers == ["agents", "codegen"]
    assert "secret" not in repr(parsed)

    for consumers in (["codegen", "agents"], ["agents", "agents"], []):
        with pytest.raises(ValidationError):
            CreateConnectionRequest.model_validate_json(
                json.dumps({**canonical, "consumers": consumers})
            )
    with pytest.raises(ValidationError):
        CreateConnectionRequest.model_validate_json(
            json.dumps({**canonical, "credential": "secret"})
        )


def test_access_contract_requires_exact_credential_version_and_execution() -> None:
    body = {
        "schema_version": "llm_credential_access_request@1",
        "project_id": "demo",
        "provider": "anthropic",
        "consumer": "agents",
        "execution_id": "attempt-1",
        "purpose": "agent.experiment.reason",
        "expected_credential_id": "10000000-0000-4000-8000-000000000001",
        "expected_credential_version": 2,
    }
    assert (
        CredentialAccessRequest.model_validate_json(json.dumps(body))
        .expected_credential_version
        == 2
    )
    for removed in ("execution_id", "expected_credential_id", "expected_credential_version"):
        invalid = dict(body)
        invalid.pop(removed)
        with pytest.raises(ValidationError):
            CredentialAccessRequest.model_validate_json(json.dumps(invalid))
