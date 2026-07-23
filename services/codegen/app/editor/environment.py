"""One canonical environment contract for production and evaluation editors."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeAlias

from app.egress import EGRESS_PROXY_ENV
from app.editor.deadlines import resolve_codegen_deadline_plan


_DEFAULT_MODEL = "claude-opus-4-8"
_MODEL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")

JsonScalar: TypeAlias = str | bool | int | float | None


PROCESS_ENV: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    *EGRESS_PROXY_ENV,
)

MODEL_PROVIDER_ENV: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "VERTEXAI_PROJECT",
    "VERTEXAI_LOCATION",
    "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
    "COHERE_API_KEY",
    "TOGETHERAI_API_KEY",
    "FIREWORKS_API_KEY",
    "XAI_API_KEY",
    "OLLAMA_API_BASE",
    "AZURE_API_KEY",
    "AZURE_API_BASE",
    "AZURE_API_VERSION",
)

MODEL_PROVIDER_CREDENTIAL_ENV: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
    "COHERE_API_KEY",
    "TOGETHERAI_API_KEY",
    "FIREWORKS_API_KEY",
    "XAI_API_KEY",
    "AZURE_API_KEY",
)

# Provider configuration that can alter where/how a model request is routed,
# without granting access to that provider. These values are publication
# identity; API keys and every other credential in MODEL_PROVIDER_ENV are not.
MODEL_PROVIDER_ROUTING_ENV: tuple[str, ...] = (
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
    "ANTHROPIC_BASE_URL",
    "VERTEXAI_PROJECT",
    "VERTEXAI_LOCATION",
    "OLLAMA_API_BASE",
    "AZURE_API_BASE",
    "AZURE_API_VERSION",
)

# Every non-secret setting that identifies the candidate or can change its
# prompt, gates, retries, or runtime behavior. Evaluation and production
# forward exactly this same tuple; the behavior fingerprint excludes revision.
CODEGEN_BEHAVIOR_ENV: tuple[str, ...] = (
    "CODEGEN_MODEL",
    "CODEGEN_HELPER_MODEL",
    "CODEGEN_REVISION",
    "CODEGEN_AIDER_BIN",
    "CODEGEN_BRIEF",
    "CODEGEN_REVIEW",
    "CODEGEN_EDIT_RETRIES",
    "CODEGEN_REQUIRE_VERIFY",
    "CODEGEN_CACHE_PROMPTS",
    "CODEGEN_CONVENTIONS",
    "CODEGEN_SDK_REFERENCE",
    "CODEGEN_CONTRACTS",
    "CODEGEN_CONTRACT_INSTALL_TIMEOUT",
    "CODEGEN_TIMEOUT",
    "CODEGEN_JOB_BUDGET",
    "CODEGEN_GIT_TIMEOUT",
    "CODEGEN_LLM_TIMEOUT",
)

EVALUATION_ENV: tuple[str, ...] = (
    *PROCESS_ENV,
    *CODEGEN_BEHAVIOR_ENV,
)


class ModelProviderConfigurationError(ValueError):
    """A configured model cannot be given one unambiguous minimal environment."""


@dataclass(frozen=True)
class _ProviderEnvironmentSpec:
    required_credentials: tuple[str, ...] = ()
    credential_choice: tuple[str, ...] = ()
    required_routing: tuple[str, ...] = ()
    optional_routing: tuple[str, ...] = ()
    exclusive_optional_routing: tuple[str, ...] = ()


_PROVIDER_ENVIRONMENT_BY_NAME: dict[str, _ProviderEnvironmentSpec] = {
    "anthropic": _ProviderEnvironmentSpec(
        required_credentials=("ANTHROPIC_API_KEY",),
        optional_routing=("ANTHROPIC_BASE_URL",),
    ),
    "azure": _ProviderEnvironmentSpec(
        required_credentials=("AZURE_API_KEY",),
        required_routing=("AZURE_API_BASE", "AZURE_API_VERSION"),
    ),
    "cohere": _ProviderEnvironmentSpec(
        required_credentials=("COHERE_API_KEY",),
    ),
    "deepseek": _ProviderEnvironmentSpec(
        required_credentials=("DEEPSEEK_API_KEY",),
    ),
    "fireworks": _ProviderEnvironmentSpec(
        required_credentials=("FIREWORKS_API_KEY",),
    ),
    "gemini": _ProviderEnvironmentSpec(
        credential_choice=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    ),
    "groq": _ProviderEnvironmentSpec(
        required_credentials=("GROQ_API_KEY",),
    ),
    "mistral": _ProviderEnvironmentSpec(
        required_credentials=("MISTRAL_API_KEY",),
    ),
    "ollama": _ProviderEnvironmentSpec(
        required_routing=("OLLAMA_API_BASE",),
    ),
    "openai": _ProviderEnvironmentSpec(
        required_credentials=("OPENAI_API_KEY",),
        exclusive_optional_routing=("OPENAI_API_BASE", "OPENAI_BASE_URL"),
    ),
    "openrouter": _ProviderEnvironmentSpec(
        required_credentials=("OPENROUTER_API_KEY",),
    ),
    "together_ai": _ProviderEnvironmentSpec(
        required_credentials=("TOGETHERAI_API_KEY",),
    ),
    "xai": _ProviderEnvironmentSpec(
        required_credentials=("XAI_API_KEY",),
    ),
}

_PROVIDER_NAME_BY_PREFIX: dict[str, str] = {
    **{name: name for name in _PROVIDER_ENVIRONMENT_BY_NAME},
    "google": "gemini",
}


def _validated_model_identifier(model: str) -> str:
    if not isinstance(model, str):
        raise ModelProviderConfigurationError("model identifier must be text")
    if _MODEL_IDENTIFIER_PATTERN.fullmatch(model) is None:
        raise ModelProviderConfigurationError(
            "model identifier must be 1 to 256 canonical model characters"
        )
    return model


def _provider_name_for_model(model: str) -> str:
    identifier = _validated_model_identifier(model)
    normalized = identifier.lower()
    prefix, separator, remainder = normalized.partition("/")
    if separator:
        if not remainder or remainder != remainder.strip():
            raise ModelProviderConfigurationError(
                f"model identifier {identifier!r} has no canonical model name"
            )
        provider = _PROVIDER_NAME_BY_PREFIX.get(prefix)
        if provider is None:
            raise ModelProviderConfigurationError(
                f"unsupported model provider prefix {prefix!r}"
            )
        return provider
    if normalized.startswith("claude"):
        return "anthropic"
    if normalized.startswith("gpt-") or normalized in {"o1", "o3", "o4"}:
        return "openai"
    if normalized.startswith(("o1-", "o3-", "o4-")):
        return "openai"
    if normalized.startswith("gemini"):
        return "gemini"
    raise ModelProviderConfigurationError(
        f"model identifier {identifier!r} does not select a supported provider"
    )


def _configured_models(
    environment: Mapping[str, str],
    *,
    model: str | None,
    helper_model: str | None,
) -> tuple[str, str]:
    main = (
        _value(environment, "CODEGEN_MODEL", _DEFAULT_MODEL) if model is None else model
    )
    helper = (
        environment.get("CODEGEN_HELPER_MODEL") or main
        if helper_model is None
        else helper_model
    )
    return _validated_model_identifier(main), _validated_model_identifier(helper)


def _present_value(environment: Mapping[str, str], name: str) -> str | None:
    value = environment.get(name)
    if value is None or not value.strip():
        return None
    if "\x00" in value:
        raise ModelProviderConfigurationError(
            f"provider environment variable {name} contains a NUL byte"
        )
    return value


def resolve_model_provider_environment(
    environment: Mapping[str, str] | None = None,
    *,
    model: str | None = None,
    helper_model: str | None = None,
    require_credentials: bool = True,
) -> dict[str, str]:
    """Select the minimal provider environment for the main and helper models.

    Unrelated provider credentials are never selected. Required credentials and
    routing must be non-empty when ``require_credentials`` is true. Alias-like
    choices (Gemini keys and OpenAI base URL variables) are accepted only when
    exactly one value can win, so a worker never inherits ambiguous provider
    state.
    """
    source: Mapping[str, str] = os.environ if environment is None else environment
    main, helper = _configured_models(
        source,
        model=model,
        helper_model=helper_model,
    )
    provider_names = tuple(
        dict.fromkeys(
            (_provider_name_for_model(main), _provider_name_for_model(helper))
        )
    )
    selected: dict[str, str] = {}
    for provider_name in provider_names:
        spec = _PROVIDER_ENVIRONMENT_BY_NAME[provider_name]
        for name in (*spec.required_credentials, *spec.required_routing):
            value = _present_value(source, name)
            if value is None:
                if require_credentials:
                    raise ModelProviderConfigurationError(
                        f"provider {provider_name!r} requires {name}"
                    )
                continue
            selected[name] = value

        if spec.credential_choice:
            choices = [
                (name, value)
                for name in spec.credential_choice
                if (value := _present_value(source, name)) is not None
            ]
            if len(choices) > 1:
                names = ", ".join(name for name, _value_ in choices)
                raise ModelProviderConfigurationError(
                    f"provider {provider_name!r} has ambiguous credentials: {names}"
                )
            if not choices:
                if require_credentials:
                    names = " or ".join(spec.credential_choice)
                    raise ModelProviderConfigurationError(
                        f"provider {provider_name!r} requires exactly one of {names}"
                    )
            else:
                name, value = choices[0]
                selected[name] = value

        for name in spec.optional_routing:
            value = _present_value(source, name)
            if value is not None:
                selected[name] = value

        routing_choices = [
            (name, value)
            for name in spec.exclusive_optional_routing
            if (value := _present_value(source, name)) is not None
        ]
        if len(routing_choices) > 1:
            names = ", ".join(name for name, _value_ in routing_choices)
            raise ModelProviderConfigurationError(
                f"provider {provider_name!r} has ambiguous routing: {names}"
            )
        if routing_choices:
            name, value = routing_choices[0]
            selected[name] = value

    return {name: selected[name] for name in sorted(selected)}


def model_provider_routing_configuration(
    environment: Mapping[str, str] | None = None,
    *,
    model: str | None = None,
    helper_model: str | None = None,
) -> dict[str, JsonScalar]:
    """Return credential-free routing identity for only the selected providers."""
    source: Mapping[str, str] = os.environ if environment is None else environment
    main, helper = _configured_models(
        source,
        model=model,
        helper_model=helper_model,
    )
    provider_names = tuple(
        dict.fromkeys(
            (_provider_name_for_model(main), _provider_name_for_model(helper))
        )
    )
    # Reuse the canonical resolver for ambiguity and value validation while
    # permitting behavior fingerprinting before credentials are provisioned.
    selected = resolve_model_provider_environment(
        source,
        model=main,
        helper_model=helper,
        require_credentials=False,
    )
    relevant_names: set[str] = set()
    for provider_name in provider_names:
        spec = _PROVIDER_ENVIRONMENT_BY_NAME[provider_name]
        relevant_names.update(spec.required_routing)
        relevant_names.update(spec.optional_routing)
        relevant_names.update(spec.exclusive_optional_routing)
    return {name: selected.get(name) for name in sorted(relevant_names)}


def _value(environment: Mapping[str, str], name: str, default: str) -> str:
    """Match ``os.getenv(name, default)`` against an explicit environment."""
    return environment[name] if name in environment else default


def _enabled(environment: Mapping[str, str], name: str) -> bool:
    """Match the Codegen config convention: only ``false`` disables a switch."""
    return _value(environment, name, "true").lower() != "false"


def normalized_codegen_behavior_configuration(
    environment: Mapping[str, str] | None = None,
) -> dict[str, JsonScalar | dict[str, JsonScalar]]:
    """Return the canonical, credential-free effective editor configuration.

    The normalization deliberately mirrors :mod:`app.config` instead of
    hashing raw environment text: boolean spelling, integer formatting, floors,
    defaults, the derived whole-job budget, and ignored legacy switches all
    collapse to the values the editor actually uses. An explicit mapping is
    resolved without mutating process-global ``os.environ``, so callers can
    safely fingerprint the exact environment forwarded to an evaluation.

    ``CODEGEN_REVISION`` is candidate provenance, not editor behavior, and is
    intentionally absent. Provider credentials are also absent; rotating a key
    must not invalidate otherwise identical evaluation evidence.
    """
    source: Mapping[str, str] = dict(os.environ) if environment is None else environment

    model = _value(source, "CODEGEN_MODEL", _DEFAULT_MODEL)
    helper_model = source.get("CODEGEN_HELPER_MODEL") or model
    edit_retries = max(0, int(_value(source, "CODEGEN_EDIT_RETRIES", "1")))
    budget_override = _value(source, "CODEGEN_JOB_BUDGET", "")
    deadline_plan = resolve_codegen_deadline_plan(
        agent_timeout_seconds=int(_value(source, "CODEGEN_TIMEOUT", "1800")),
        git_timeout_seconds=int(_value(source, "CODEGEN_GIT_TIMEOUT", "300")),
        llm_timeout_seconds=float(_value(source, "CODEGEN_LLM_TIMEOUT", "240")),
        edit_retries=edit_retries,
        brief_enabled=_enabled(source, "CODEGEN_BRIEF"),
        review_enabled=_enabled(source, "CODEGEN_REVIEW"),
        job_budget_override=(int(budget_override) if budget_override.strip() else None),
    )

    provider_routing = model_provider_routing_configuration(
        source,
        model=model,
        helper_model=helper_model,
    )

    return {
        "schema_version": "codegen_behavior_configuration@2",
        "model": model,
        "helper_model": helper_model,
        "aider_bin": _value(source, "CODEGEN_AIDER_BIN", "aider"),
        "brief_enabled": _enabled(source, "CODEGEN_BRIEF"),
        "review_enabled": _enabled(source, "CODEGEN_REVIEW"),
        "edit_retries": edit_retries,
        "cache_prompts": _enabled(source, "CODEGEN_CACHE_PROMPTS"),
        "conventions_enabled": _enabled(source, "CODEGEN_CONVENTIONS"),
        # app.config intentionally disables static cross-version SDK guidance.
        "sdk_reference_enabled": False,
        "contracts_enabled": _enabled(source, "CODEGEN_CONTRACTS"),
        "contract_install_timeout_seconds": max(
            1,
            int(_value(source, "CODEGEN_CONTRACT_INSTALL_TIMEOUT", "600")),
        ),
        "agent_timeout_seconds": deadline_plan.agent_timeout_seconds,
        "git_timeout_seconds": deadline_plan.git_timeout_seconds,
        "llm_timeout_seconds": deadline_plan.llm_timeout_seconds,
        "job_budget_seconds": deadline_plan.job_budget_seconds,
        # GitHub CI is authoritative; app.config ignores this legacy switch.
        "require_verify": False,
        "provider_routing": provider_routing,
    }


def codegen_behavior_configuration_sha256(
    environment: Mapping[str, str] | None = None,
) -> str:
    """Hash the canonical effective behavior configuration as strict JSON."""
    payload = normalized_codegen_behavior_configuration(environment)
    canonical = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
