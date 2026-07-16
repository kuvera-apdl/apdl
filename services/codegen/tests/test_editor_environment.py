"""Canonical Codegen editor behavior identity."""

from __future__ import annotations

import json

import pytest

from app import config
from app.editor.environment import (
    CODEGEN_BEHAVIOR_ENV,
    MODEL_PROVIDER_ENV,
    MODEL_PROVIDER_ROUTING_ENV,
    codegen_behavior_configuration_sha256,
    normalized_codegen_behavior_configuration,
)


def _replace_relevant_environment(monkeypatch, environment: dict[str, str]) -> None:
    relevant = {
        *CODEGEN_BEHAVIOR_ENV,
        *MODEL_PROVIDER_ENV,
        "GIT_COMMIT_SHA",
    }
    for name in relevant:
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)


def _assert_matches_config_getters(configuration: dict) -> None:
    assert configuration["model"] == config.codegen_model()
    assert configuration["helper_model"] == config.codegen_helper_model()
    assert configuration["aider_bin"] == config.codegen_aider_bin()
    assert configuration["brief_enabled"] == config.codegen_brief_enabled()
    assert configuration["review_enabled"] == config.codegen_review_enabled()
    assert configuration["edit_retries"] == config.codegen_edit_retries()
    assert configuration["cache_prompts"] == config.codegen_cache_prompts()
    assert (
        configuration["conventions_enabled"]
        == config.codegen_conventions_enabled()
    )
    assert (
        configuration["sdk_reference_enabled"]
        == config.codegen_sdk_reference_enabled()
    )
    assert configuration["contracts_enabled"] == config.codegen_contracts_enabled()
    assert (
        configuration["contract_install_timeout_seconds"]
        == config.codegen_contract_install_timeout()
    )
    assert configuration["agent_timeout_seconds"] == config.codegen_agent_timeout()
    assert configuration["git_timeout_seconds"] == config.codegen_git_timeout()
    assert configuration["llm_timeout_seconds"] == config.codegen_llm_timeout()
    assert configuration["job_budget_seconds"] == config.codegen_job_budget()
    assert configuration["require_verify"] == config.codegen_require_verify()


def test_default_configuration_matches_effective_app_config(monkeypatch) -> None:
    _replace_relevant_environment(monkeypatch, {})

    configuration = normalized_codegen_behavior_configuration({})

    _assert_matches_config_getters(configuration)
    assert configuration == {
        "schema_version": "codegen_behavior_configuration@1",
        "model": "claude-opus-4-8",
        "helper_model": "claude-opus-4-8",
        "aider_bin": "aider",
        "brief_enabled": True,
        "review_enabled": True,
        "edit_retries": 1,
        "cache_prompts": True,
        "conventions_enabled": True,
        "sdk_reference_enabled": False,
        "contracts_enabled": True,
        "contract_install_timeout_seconds": 600,
        "agent_timeout_seconds": 1800,
        "git_timeout_seconds": 300,
        "llm_timeout_seconds": 240.0,
        "job_budget_seconds": 3000,
        "require_verify": False,
        "provider_routing": {
            name: None for name in MODEL_PROVIDER_ROUTING_ENV
        },
    }


def test_normalization_matches_effective_app_config(monkeypatch) -> None:
    environment = {
        "CODEGEN_MODEL": "openai/gpt-5",
        "CODEGEN_HELPER_MODEL": "",
        "CODEGEN_AIDER_BIN": "/opt/aider",
        "CODEGEN_BRIEF": "FALSE",
        "CODEGEN_REVIEW": "anything-else-is-enabled",
        "CODEGEN_EDIT_RETRIES": "-4",
        "CODEGEN_CACHE_PROMPTS": "FaLsE",
        "CODEGEN_CONVENTIONS": "false",
        "CODEGEN_SDK_REFERENCE": "true",
        "CODEGEN_CONTRACTS": "FALSE",
        "CODEGEN_CONTRACT_INSTALL_TIMEOUT": "0",
        "CODEGEN_TIMEOUT": "090",
        "CODEGEN_GIT_TIMEOUT": "030",
        "CODEGEN_LLM_TIMEOUT": "12.50",
        "CODEGEN_JOB_BUDGET": "00080",
        "CODEGEN_REQUIRE_VERIFY": "true",
        "OPENAI_BASE_URL": "https://router.example/v1",
    }
    _replace_relevant_environment(monkeypatch, environment)

    configuration = normalized_codegen_behavior_configuration(environment)

    _assert_matches_config_getters(configuration)
    assert configuration["helper_model"] == "openai/gpt-5"
    assert configuration["edit_retries"] == 0
    assert configuration["contract_install_timeout_seconds"] == 1
    assert configuration["agent_timeout_seconds"] == 90
    assert configuration["git_timeout_seconds"] == 30
    assert configuration["llm_timeout_seconds"] == 12.5
    assert configuration["job_budget_seconds"] == 80
    assert configuration["sdk_reference_enabled"] is False
    assert configuration["require_verify"] is False


@pytest.mark.parametrize(
    "name,value",
    [
        ("CODEGEN_MODEL", "openai/gpt-5"),
        ("CODEGEN_HELPER_MODEL", "openai/gpt-5-mini"),
        ("CODEGEN_AIDER_BIN", "/opt/aider"),
        ("CODEGEN_BRIEF", "false"),
        ("CODEGEN_REVIEW", "false"),
        ("CODEGEN_EDIT_RETRIES", "2"),
        ("CODEGEN_CACHE_PROMPTS", "false"),
        ("CODEGEN_CONVENTIONS", "false"),
        ("CODEGEN_CONTRACTS", "false"),
        ("CODEGEN_CONTRACT_INSTALL_TIMEOUT", "601"),
        ("CODEGEN_TIMEOUT", "1799"),
        ("CODEGEN_GIT_TIMEOUT", "299"),
        ("CODEGEN_LLM_TIMEOUT", "239"),
        ("CODEGEN_JOB_BUDGET", "2999"),
        ("OPENAI_API_BASE", "https://gateway.example/v1"),
        ("VERTEXAI_PROJECT", "evaluation-project"),
        ("AZURE_API_VERSION", "2026-07-01"),
    ],
)
def test_effective_behavior_changes_alter_fingerprint(name: str, value: str) -> None:
    baseline = codegen_behavior_configuration_sha256({})

    assert codegen_behavior_configuration_sha256({name: value}) != baseline


def test_ignored_or_equivalent_values_do_not_alter_fingerprint() -> None:
    baseline = codegen_behavior_configuration_sha256({})

    assert codegen_behavior_configuration_sha256(
        {
            "CODEGEN_SDK_REFERENCE": "true",
            "CODEGEN_REQUIRE_VERIFY": "true",
            "OPENAI_API_BASE": "",
        }
    ) == baseline


def test_secret_rotation_and_revision_do_not_alter_fingerprint() -> None:
    credential_names = set(MODEL_PROVIDER_ENV) - set(MODEL_PROVIDER_ROUTING_ENV)
    first = {name: f"first-secret-{index}" for index, name in enumerate(credential_names)}
    second = {
        name: f"rotated-secret-{index}" for index, name in enumerate(credential_names)
    }
    first.update(
        {
            "CODEGEN_REVISION": "revision-one",
            "GIT_COMMIT_SHA": "commit-one",
        }
    )
    second.update(
        {
            "CODEGEN_REVISION": "revision-two",
            "GIT_COMMIT_SHA": "commit-two",
        }
    )

    first_configuration = normalized_codegen_behavior_configuration(first)
    second_configuration = normalized_codegen_behavior_configuration(second)

    assert first_configuration == second_configuration
    assert codegen_behavior_configuration_sha256(
        first
    ) == codegen_behavior_configuration_sha256(second)
    serialized = json.dumps(first_configuration, sort_keys=True)
    assert "secret" not in serialized
    assert "revision-one" not in serialized
    assert len(codegen_behavior_configuration_sha256(first)) == 64
