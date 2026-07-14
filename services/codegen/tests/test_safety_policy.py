"""Strict authority and resolution tests for Codegen safety policy."""

import json

import pytest
from pydantic import ValidationError

from app.runtime.models import RUNTIME_ACCEPTANCE_WORKFLOW_PATH
from app.safety.policy import (
    DEFAULT_PROTECTED_PATTERNS,
    MAX_ADDITIONAL_PROTECTED_PATHS,
    PlatformCodegenSafetyPolicy,
    TenantCodegenConnectionPolicy,
    VerifiedProtectedPathExemption,
    load_platform_safety_policy,
    resolve_effective_policy,
    validate_tenant_policy_against_platform,
)


def test_default_tenant_policy_has_one_canonical_versioned_shape():
    policy = TenantCodegenConnectionPolicy()

    assert policy.model_dump(mode="json") == {
        "schema_version": "tenant_codegen_connection_policy@1",
        "test_cmd": None,
        "gates": {
            "max_files": None,
            "max_lines": None,
            "additional_protected_paths": [],
        },
        "runtime_acceptance": {
            "schema_version": "runtime_acceptance_request@1",
            "enabled": False,
        },
    }


def test_tenant_policy_rejects_legacy_replacement_and_allowlist_fields():
    for field in ("protected_paths", "allowed_protected_paths"):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            TenantCodegenConnectionPolicy.model_validate(
                {
                    "schema_version": "tenant_codegen_connection_policy@1",
                    "gates": {field: []},
                }
            )


def test_tenant_policy_is_strict_and_canonicalizes_additional_paths():
    with pytest.raises(ValidationError):
        TenantCodegenConnectionPolicy.model_validate(
            {"gates": {"max_files": "10"}}
        )
    with pytest.raises(ValidationError):
        TenantCodegenConnectionPolicy.model_validate(
            {"gates": {"additional_protected_paths": ["../secrets/**"]}}
        )
    with pytest.raises(ValidationError, match="must not be blank"):
        TenantCodegenConnectionPolicy(test_cmd="   ")

    policy = TenantCodegenConnectionPolicy.model_validate(
        {
            "gates": {
                "additional_protected_paths": ["infra/**", "src/generated/**", "infra/**"]
            }
        }
    )
    assert policy.gates.additional_protected_paths == [
        "infra/**",
        "src/generated/**",
    ]


def test_additional_protected_path_lists_are_bounded():
    paths = [f"protected/{index}/**" for index in range(MAX_ADDITIONAL_PROTECTED_PATHS + 1)]
    with pytest.raises(ValidationError, match="at most 64 items"):
        TenantCodegenConnectionPolicy.model_validate(
            {"gates": {"additional_protected_paths": paths}}
        )
    with pytest.raises(ValidationError, match="at most 64 items"):
        PlatformCodegenSafetyPolicy(additional_protected_paths=paths)


def test_resolver_uses_union_min_and_intersects_runtime_authority():
    platform = PlatformCodegenSafetyPolicy(
        max_files=40,
        max_lines=1500,
        additional_protected_paths=["operator/**"],
        runtime_workflow_generation_enabled=True,
    )
    tenant = TenantCodegenConnectionPolicy.model_validate(
        {
            "gates": {
                "max_files": 12,
                "max_lines": 900,
                "additional_protected_paths": ["tenant/**"],
            },
            "runtime_acceptance": {
                "schema_version": "runtime_acceptance_request@1",
                "enabled": True,
            },
        }
    )

    effective = resolve_effective_policy(tenant, platform)

    assert effective.max_files == 12
    assert effective.max_lines == 900
    assert set(DEFAULT_PROTECTED_PATTERNS).issubset(effective.protected_paths)
    assert {"operator/**", "tenant/**"}.issubset(effective.protected_paths)
    assert effective.runtime_workflow_generation_enabled is True


def test_resolver_defensively_clamps_permissive_stored_policy():
    platform = PlatformCodegenSafetyPolicy(max_files=20, max_lines=800)
    tenant = TenantCodegenConnectionPolicy.model_validate(
        {"gates": {"max_files": 10_000_000, "max_lines": 10_000_000}}
    )

    effective = resolve_effective_policy(tenant, platform)
    assert effective.max_files == 20
    assert effective.max_lines == 800

    with pytest.raises(ValueError, match="max_files"):
        validate_tenant_policy_against_platform(tenant, platform)


def test_runtime_workflow_requires_both_tenant_opt_in_and_operator_grant():
    opted_in = TenantCodegenConnectionPolicy.model_validate(
        {"runtime_acceptance": {"enabled": True}}
    )
    assert (
        resolve_effective_policy(opted_in, PlatformCodegenSafetyPolicy())
        .runtime_workflow_generation_enabled
        is False
    )
    assert (
        resolve_effective_policy(
            TenantCodegenConnectionPolicy(),
            PlatformCodegenSafetyPolicy(
                runtime_workflow_generation_enabled=True
            ),
        ).runtime_workflow_generation_enabled
        is False
    )


def test_effective_policy_digest_is_canonical_for_equivalent_inputs():
    first = resolve_effective_policy(
        TenantCodegenConnectionPolicy.model_validate(
            {"gates": {"additional_protected_paths": ["z/**", "a/**"]}}
        ),
        PlatformCodegenSafetyPolicy(),
    )
    second = resolve_effective_policy(
        TenantCodegenConnectionPolicy.model_validate(
            {"gates": {"additional_protected_paths": ["a/**", "z/**", "a/**"]}}
        ),
        PlatformCodegenSafetyPolicy(),
    )

    assert first.canonical_digest() == second.canonical_digest()
    assert len(first.canonical_digest()) == 64


def test_platform_policy_loader_uses_safe_defaults(monkeypatch):
    monkeypatch.delenv("CODEGEN_PLATFORM_SAFETY_POLICY_PATH", raising=False)

    policy = load_platform_safety_policy()

    assert policy.max_files == 50
    assert policy.max_lines == 2000
    assert policy.runtime_workflow_generation_enabled is False


def test_platform_policy_loader_accepts_only_absolute_strict_json(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEGEN_PLATFORM_SAFETY_POLICY_PATH", "relative.json")
    with pytest.raises(RuntimeError, match="absolute path"):
        load_platform_safety_policy()

    policy_path = tmp_path / "platform-policy.json"
    policy_path.write_text(json.dumps({"max_files": "20"}), encoding="utf-8")
    monkeypatch.setenv("CODEGEN_PLATFORM_SAFETY_POLICY_PATH", str(policy_path))
    with pytest.raises(RuntimeError, match="valid strict"):
        load_platform_safety_policy()

    policy_path.write_text(
        json.dumps(
            {
                "schema_version": "platform_codegen_safety_policy@1",
                "max_files": 20,
                "max_lines": 800,
                "additional_protected_paths": ["infra/**"],
                "runtime_workflow_generation_enabled": True,
            }
        ),
        encoding="utf-8",
    )
    loaded = load_platform_safety_policy()
    assert loaded.max_files == 20
    assert loaded.additional_protected_paths == ["infra/**"]
    assert loaded.runtime_workflow_generation_enabled is True
    assert resolve_effective_policy(TenantCodegenConnectionPolicy()).max_files == 20


def test_verified_exemption_cannot_name_an_arbitrary_workflow():
    with pytest.raises(ValidationError):
        VerifiedProtectedPathExemption(
            path=".github/workflows/ci.yml",
            content_sha256="a" * 64,
            runtime_acceptance_plan_sha256="b" * 64,
        )

    exemption = VerifiedProtectedPathExemption(
        path=RUNTIME_ACCEPTANCE_WORKFLOW_PATH,
        content_sha256="a" * 64,
        runtime_acceptance_plan_sha256="b" * 64,
    )
    assert exemption.path == RUNTIME_ACCEPTANCE_WORKFLOW_PATH
