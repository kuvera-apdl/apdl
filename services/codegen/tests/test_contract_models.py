"""Strict-schema tests for Phase 2 dependency contract boundaries."""

import pytest
from pydantic import ValidationError

from app.contracts.models import (
    CompileCheckedExample,
    ContractRequest,
    ContractResolution,
)


def test_contract_request_rejects_unknown_fields_and_schema_versions():
    payload = {
        "ecosystem": "node",
        "package_path": ".",
        "package_name": "widget-sdk",
        "exact_version": "1.2.3",
        "manifest_path": "package.json",
        "lockfile_path": "package-lock.json",
    }
    with pytest.raises(ValidationError):
        ContractRequest.model_validate({**payload, "package": "alias"})
    with pytest.raises(ValidationError):
        ContractRequest.model_validate(
            {**payload, "schema_version": "contract_request@2"}
        )


def test_contract_request_normalizes_order_only_not_field_names():
    request = ContractRequest(
        ecosystem="python",
        package_path=".",
        package_name="demo-sdk",
        exact_version="2.0.0",
        manifest_path="pyproject.toml",
        lockfile_path="uv.lock",
        requirement_ids=["req-b", "req-a", "req-a"],
        symbols=["Client", "connect", "Client"],
    )
    assert request.requirement_ids == ["req-a", "req-b"]
    assert request.symbols == ["Client", "connect"]


def test_unchecked_example_cannot_enter_contract_evidence():
    payload = {
        "language": "TypeScript",
        "snippet": "import { x } from 'pkg';",
        "command": "tsc --noEmit",
        "tool_version": "5.7.2",
        "output_sha256": "0" * 64,
        "source_ids": ["1" * 64],
    }
    with pytest.raises(ValidationError):
        CompileCheckedExample.model_validate({**payload, "check_result": "failed"})


def test_ready_resolution_cannot_claim_success_without_evidence():
    request = ContractRequest(
        ecosystem="node",
        package_path=".",
        package_name="widget-sdk",
        exact_version="1.2.3",
        manifest_path="package.json",
        lockfile_path="package-lock.json",
    )
    with pytest.raises(ValidationError):
        ContractResolution(
            request=request,
            disposition="ready",
            evidence=None,
        )
