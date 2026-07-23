"""Canonical Codegen editor behavior identity."""

from __future__ import annotations

import json

import pytest

from app import config
from app.editor.environment import (
    CODEGEN_BEHAVIOR_ENV,
    MODEL_PROVIDER_ENV,
    MODEL_PROVIDER_ROUTING_ENV,
    ModelProviderConfigurationError,
    codegen_behavior_configuration_sha256,
    normalized_codegen_behavior_configuration,
    resolve_model_provider_environment,
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
    assert configuration["conventions_enabled"] == config.codegen_conventions_enabled()
    assert (
        configuration["sdk_reference_enabled"] == config.codegen_sdk_reference_enabled()
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
        "schema_version": "codegen_behavior_configuration@2",
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
        "agent_timeout_seconds": 1075,
        "git_timeout_seconds": 179,
        "llm_timeout_seconds": 143.41463414634146,
        "job_budget_seconds": 3000,
        "require_verify": False,
        "provider_routing": {"ANTHROPIC_BASE_URL": None},
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
    assert configuration["agent_timeout_seconds"] == 11
    assert configuration["git_timeout_seconds"] == 3
    assert configuration["llm_timeout_seconds"] == pytest.approx(1.5384615385)
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
    ],
)
def test_effective_behavior_changes_alter_fingerprint(name: str, value: str) -> None:
    baseline = codegen_behavior_configuration_sha256({})

    assert codegen_behavior_configuration_sha256({name: value}) != baseline


def test_ignored_or_equivalent_values_do_not_alter_fingerprint() -> None:
    baseline = codegen_behavior_configuration_sha256({})

    assert (
        codegen_behavior_configuration_sha256(
            {
                "CODEGEN_SDK_REFERENCE": "true",
                "CODEGEN_REQUIRE_VERIFY": "true",
                "OPENAI_API_BASE": "",
                "VERTEXAI_PROJECT": "unrelated-project",
                "AZURE_API_VERSION": "unrelated-version",
            }
        )
        == baseline
    )


@pytest.mark.parametrize(
    ("model", "routing_name", "routing_value"),
    [
        ("openai/gpt-5", "OPENAI_API_BASE", "https://gateway.example/v1"),
        ("azure/gpt-5", "AZURE_API_VERSION", "2026-07-01"),
        ("ollama/qwen3", "OLLAMA_API_BASE", "http://model-host:11434"),
    ],
)
def test_selected_provider_routing_alters_fingerprint(
    model: str,
    routing_name: str,
    routing_value: str,
) -> None:
    baseline = codegen_behavior_configuration_sha256({"CODEGEN_MODEL": model})

    assert (
        codegen_behavior_configuration_sha256(
            {"CODEGEN_MODEL": model, routing_name: routing_value}
        )
        != baseline
    )


def test_secret_rotation_and_revision_do_not_alter_fingerprint() -> None:
    credential_names = set(MODEL_PROVIDER_ENV) - set(MODEL_PROVIDER_ROUTING_ENV)
    first = {
        name: f"first-secret-{index}" for index, name in enumerate(credential_names)
    }
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


def test_provider_environment_is_the_exact_main_and_helper_union() -> None:
    environment = {
        "CODEGEN_MODEL": "anthropic/claude-opus-4-8",
        "CODEGEN_HELPER_MODEL": "openai/gpt-5-mini",
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "ANTHROPIC_BASE_URL": "https://anthropic-gateway.example/v1",
        "OPENAI_API_KEY": "openai-secret",
        "OPENAI_BASE_URL": "https://openai-gateway.example/v1",
        "GROQ_API_KEY": "unrelated-secret",
        "VERTEXAI_PROJECT": "unrelated-project",
    }

    assert resolve_model_provider_environment(environment) == {
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "ANTHROPIC_BASE_URL": "https://anthropic-gateway.example/v1",
        "OPENAI_API_KEY": "openai-secret",
        "OPENAI_BASE_URL": "https://openai-gateway.example/v1",
    }


@pytest.mark.parametrize(
    ("model", "provider_environment", "expected"),
    [
        (
            "gemini/gemini-2.5-pro",
            {"GEMINI_API_KEY": "gemini-secret"},
            {"GEMINI_API_KEY": "gemini-secret"},
        ),
        (
            "google/gemini-2.5-pro",
            {"GOOGLE_API_KEY": "google-secret"},
            {"GOOGLE_API_KEY": "google-secret"},
        ),
        (
            "azure/gpt-5",
            {
                "AZURE_API_KEY": "azure-secret",
                "AZURE_API_BASE": "https://azure.example/v1",
                "AZURE_API_VERSION": "2026-07-01",
            },
            {
                "AZURE_API_KEY": "azure-secret",
                "AZURE_API_BASE": "https://azure.example/v1",
                "AZURE_API_VERSION": "2026-07-01",
            },
        ),
        (
            "ollama/qwen3",
            {"OLLAMA_API_BASE": "http://model-host:11434"},
            {"OLLAMA_API_BASE": "http://model-host:11434"},
        ),
    ],
)
def test_provider_environment_resolves_supported_provider_contracts(
    model: str,
    provider_environment: dict[str, str],
    expected: dict[str, str],
) -> None:
    environment = {"CODEGEN_MODEL": model, **provider_environment}

    assert resolve_model_provider_environment(environment) == expected


@pytest.mark.parametrize(
    ("model", "credential_name"),
    [
        ("claude-opus-4-8", "ANTHROPIC_API_KEY"),
        ("gpt-5", "OPENAI_API_KEY"),
        ("cohere/command-r-plus", "COHERE_API_KEY"),
        ("deepseek/deepseek-chat", "DEEPSEEK_API_KEY"),
        ("fireworks/accounts/example/models/demo", "FIREWORKS_API_KEY"),
        ("groq/llama-3.3-70b-versatile", "GROQ_API_KEY"),
        ("mistral/mistral-large-latest", "MISTRAL_API_KEY"),
        ("openrouter/anthropic/claude-sonnet-4", "OPENROUTER_API_KEY"),
        ("together_ai/meta-llama/Llama-3.3-70B", "TOGETHERAI_API_KEY"),
        ("xai/grok-4", "XAI_API_KEY"),
    ],
)
def test_provider_environment_covers_each_single_credential_provider(
    model: str,
    credential_name: str,
) -> None:
    environment = {
        "CODEGEN_MODEL": model,
        credential_name: "selected-provider-secret",
        "GEMINI_API_KEY": "unrelated-provider-secret",
    }

    assert resolve_model_provider_environment(environment) == {
        credential_name: "selected-provider-secret"
    }


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        (
            {"CODEGEN_MODEL": "openai/gpt-5"},
            "requires OPENAI_API_KEY",
        ),
        (
            {
                "CODEGEN_MODEL": "anthropic/claude-opus-4-8",
                "CODEGEN_HELPER_MODEL": "openai/gpt-5-mini",
                "ANTHROPIC_API_KEY": "anthropic-secret",
            },
            "requires OPENAI_API_KEY",
        ),
        (
            {
                "CODEGEN_MODEL": "gemini/gemini-2.5-pro",
                "GOOGLE_API_KEY": "google-secret",
                "GEMINI_API_KEY": "gemini-secret",
            },
            "ambiguous credentials",
        ),
        (
            {
                "CODEGEN_MODEL": "openai/gpt-5",
                "OPENAI_API_KEY": "openai-secret",
                "OPENAI_API_BASE": "https://one.example/v1",
                "OPENAI_BASE_URL": "https://two.example/v1",
            },
            "ambiguous routing",
        ),
        (
            {
                "CODEGEN_MODEL": "vertex_ai/gemini-2.5-pro",
                "VERTEXAI_PROJECT": "project",
                "VERTEXAI_LOCATION": "region",
            },
            "unsupported model provider",
        ),
        (
            {
                "CODEGEN_MODEL": "custom-unqualified-model",
                "OPENAI_API_KEY": "openai-secret",
            },
            "does not select a supported provider",
        ),
        (
            {
                "CODEGEN_MODEL": 'openai/gpt-5"\nuse_temperature: true',
                "OPENAI_API_KEY": "openai-secret",
            },
            "canonical model characters",
        ),
        (
            {
                "CODEGEN_MODEL": "openai/" + "x" * 256,
                "OPENAI_API_KEY": "openai-secret",
            },
            "1 to 256",
        ),
    ],
)
def test_provider_environment_fails_closed_for_incomplete_or_ambiguous_state(
    environment: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ModelProviderConfigurationError, match=message):
        resolve_model_provider_environment(environment)
