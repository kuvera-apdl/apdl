"""Multi-provider LLM router with fallback — OpenAI, Anthropic, Google, and local models.

Tier 1 (fast): High-throughput, lower-cost tasks — summarisation, classification,
               UI config generation.
Tier 2 (reasoning): Complex analysis, experiment design, feature proposals.

Each tier tries providers in order and falls back on failure.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import anthropic
import openai
from google import genai
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

#: Per-request timeout. Without one, SDK defaults (~10 min) mean a hung
#: provider stalls an agent run for up to 10 minutes per fallback rung.
_REQUEST_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "120"))

# ---------------------------------------------------------------------------
# Lazy-initialised clients
# ---------------------------------------------------------------------------

_openai_client: openai.AsyncOpenAI | None = None
_anthropic_client: anthropic.AsyncAnthropic | None = None
_google_client: genai.Client | None = None
_local_client: openai.AsyncOpenAI | None = None


def _get_openai() -> openai.AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY", ""), timeout=_REQUEST_TIMEOUT
        )
    return _openai_client


def _get_anthropic() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY", ""), timeout=_REQUEST_TIMEOUT
        )
    return _anthropic_client


def _get_google() -> genai.Client:
    global _google_client
    if _google_client is None:
        _google_client = genai.Client(
            api_key=os.getenv("GOOGLE_API_KEY", ""),
            http_options=genai_types.HttpOptions(timeout=int(_REQUEST_TIMEOUT * 1000)),
        )
    return _google_client


def _get_local() -> openai.AsyncOpenAI:
    global _local_client
    if _local_client is None:
        _local_client = openai.AsyncOpenAI(
            base_url=os.getenv("LOCAL_LLM_URL", "http://localhost:11434/v1"),
            api_key="local",  # local servers don't require a real key
            timeout=_REQUEST_TIMEOUT,
        )
    return _local_client


# ---------------------------------------------------------------------------
# Tier → ordered model list
# ---------------------------------------------------------------------------

_TIER_DEFAULTS: dict[str, dict[str, Any]] = {
    "fast": {"max_tokens": 4096, "temperature": 0.3},
    "reasoning": {"max_tokens": 8192, "temperature": 0.2},
}


def _tier_models(tier: str) -> list[dict[str, str]]:
    """Return an ordered list of (provider, model) pairs for the given tier.

    Providers whose API key is not set are skipped (except local, which
    is always available when LOCAL_LLM_URL is configured).
    """
    if tier == "fast":
        candidates = [
            ("openai", os.getenv("LLM_FAST_PRIMARY", "gpt-5.4-nano")),
            ("anthropic", os.getenv("LLM_FAST_FALLBACK", "claude-haiku-4-5-20251001")),
            ("google", os.getenv("LLM_FAST_GOOGLE", "gemini-2.5-flash-lite")),
            ("local", os.getenv("LOCAL_LLM_MODEL", "gemma4")),
        ]
    else:
        candidates = [
            ("openai", os.getenv("LLM_REASONING_PRIMARY", "gpt-5.4-mini")),
            ("anthropic", os.getenv("LLM_REASONING_FALLBACK", "claude-sonnet-4-6")),
            ("google", os.getenv("LLM_REASONING_GOOGLE", "gemini-2.5-flash")),
            ("local", os.getenv("LOCAL_LLM_MODEL", "gemma4")),
        ]

    result: list[dict[str, str]] = []
    for provider, model in candidates:
        if _provider_available(provider):
            result.append({"provider": provider, "model": model})
    return result


def _provider_available(provider: str) -> bool:
    if provider == "openai":
        return bool(os.getenv("OPENAI_API_KEY"))
    if provider == "anthropic":
        return bool(os.getenv("ANTHROPIC_API_KEY"))
    if provider == "google":
        return bool(os.getenv("GOOGLE_API_KEY"))
    if provider == "local":
        # Available when explicitly configured OR as last-resort fallback
        return bool(os.getenv("LOCAL_LLM_URL"))
    return False


# ---------------------------------------------------------------------------
# Provider-specific completion functions
# ---------------------------------------------------------------------------

def _is_openai_reasoning_model(model: str) -> bool:
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


async def _openai_completion(
    model: str, messages: list[dict[str, str]], **kwargs: Any,
) -> str:
    client = _get_openai()
    if _is_openai_reasoning_model(model):
        # Reasoning-family models 400 on `max_tokens` (they take
        # `max_completion_tokens`) and on any non-default temperature — the
        # tier defaults would otherwise make this rung fail on every call.
        if "max_tokens" in kwargs:
            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
        kwargs.pop("temperature", None)
    resp = await client.chat.completions.create(model=model, messages=messages, **kwargs)
    return resp.choices[0].message.content or ""


async def _anthropic_completion(
    model: str, messages: list[dict[str, str]], **kwargs: Any,
) -> str:
    client = _get_anthropic()
    system_text = ""
    chat_messages: list[dict[str, str]] = []
    for msg in messages:
        if msg["role"] == "system":
            system_text = msg["content"]
        else:
            chat_messages.append(msg)

    max_tokens = kwargs.pop("max_tokens", 4096)
    resp = await client.messages.create(
        model=model,
        system=system_text,
        messages=chat_messages,
        max_tokens=max_tokens,
        **kwargs,
    )
    # content can be empty (e.g. refusal) or lead with a non-text block —
    # indexing [0].text would raise and misreport it as a provider failure.
    parts = [block.text for block in resp.content if getattr(block, "text", None)]
    return "\n".join(parts)


async def _google_completion(
    model: str, messages: list[dict[str, str]], **kwargs: Any,
) -> str:
    client = _get_google()
    system_instruction: str | None = None
    contents: list[dict[str, Any]] = []
    for msg in messages:
        if msg["role"] == "system":
            system_instruction = msg["content"]
        else:
            role = "model" if msg["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    config = genai_types.GenerateContentConfig(
        max_output_tokens=kwargs.get("max_tokens", 4096),
        temperature=kwargs.get("temperature"),
        system_instruction=system_instruction,
    )

    resp = await client.aio.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )
    return resp.text or ""


async def _local_completion(
    model: str, messages: list[dict[str, str]], **kwargs: Any,
) -> str:
    client = _get_local()
    resp = await client.chat.completions.create(model=model, messages=messages, **kwargs)
    return resp.choices[0].message.content or ""


_PROVIDER_FN = {
    "openai": _openai_completion,
    "anthropic": _anthropic_completion,
    "google": _google_completion,
    "local": _local_completion,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def chat_completion(
    model_tier: str,
    messages: list[dict[str, str]],
    **kwargs: Any,
) -> str:
    """Route a chat completion through the configured provider chain.

    Tries each provider in order for the given tier, falling back on failure.

    Args:
        model_tier: "fast" or "reasoning"
        messages: Chat messages in OpenAI format (role + content)
        **kwargs: Additional parameters forwarded to the provider

    Returns:
        The assistant's response text.

    Raises:
        RuntimeError: If all providers fail.
    """
    if model_tier not in _TIER_DEFAULTS:
        raise ValueError(
            f"Unknown model_tier {model_tier!r} — expected one of {sorted(_TIER_DEFAULTS)}"
        )

    models = _tier_models(model_tier)
    if not models:
        raise RuntimeError(
            f"No LLM providers configured for tier '{model_tier}'. "
            "Set OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY, or LOCAL_LLM_URL."
        )

    defaults = _TIER_DEFAULTS.get(model_tier, {})
    merged = {**defaults, **kwargs}

    last_exc: Exception | None = None
    for entry in models:
        provider = entry["provider"]
        model = entry["model"]
        fn = _PROVIDER_FN[provider]
        try:
            result = await fn(model, messages, **merged)
            # Visible at info level so a permanently-failing primary (every
            # call silently landing on a fallback rung) is observable.
            logger.info("LLM call ok (provider=%s, model=%s)", provider, model)
            return result
        except Exception as exc:
            logger.warning("LLM call failed (provider=%s, model=%s): %s", provider, model, exc)
            last_exc = exc

    raise RuntimeError(f"All LLM providers failed for tier '{model_tier}'") from last_exc
