"""One canonical environment contract for production and evaluation editors."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from typing import TypeAlias

from app.editor.deadlines import resolve_codegen_deadline_plan


_DEFAULT_MODEL = "claude-opus-4-8"

JsonScalar: TypeAlias = str | bool | int | float | None


PROCESS_ENV: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
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
    *MODEL_PROVIDER_ENV,
    *CODEGEN_BEHAVIOR_ENV,
)


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
        llm_timeout_seconds=float(
            _value(source, "CODEGEN_LLM_TIMEOUT", "240")
        ),
        edit_retries=edit_retries,
        brief_enabled=_enabled(source, "CODEGEN_BRIEF"),
        review_enabled=_enabled(source, "CODEGEN_REVIEW"),
        job_budget_override=(
            int(budget_override) if budget_override.strip() else None
        ),
    )

    # Empty routing variables are equivalent to their unset Compose/default
    # representation. Preserve all other text exactly: whitespace or a changed
    # endpoint can change provider behavior and therefore must change identity.
    provider_routing: dict[str, JsonScalar] = {
        name: source.get(name) or None for name in MODEL_PROVIDER_ROUTING_ENV
    }

    return {
        "schema_version": "codegen_behavior_configuration@1",
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
