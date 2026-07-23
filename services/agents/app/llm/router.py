"""Multi-provider LLM router with fallback — OpenAI, Anthropic, Google, and local models.

Tier 1 (fast): High-throughput, lower-cost tasks — summarisation, classification,
               UI config generation.
Tier 2 (reasoning): Complex analysis, experiment design, feature proposals.

Each request is authorized, budgeted, and recorded in PostgreSQL before any
provider egress. Fallback happens only for classified retryable failures and
may cross a vendor boundary only when the project's explicit policy permits it.

Two entry points share the tier/fallback machinery:

* :func:`chat_completion` — plain text in/out (the original API).
* :func:`chat_completion_with_tools` — function calling. Conversations use the
  OpenAI wire shape as the neutral format (assistant messages may carry
  ``tool_calls``; tool results are ``{"role": "tool", ...}`` messages) and are
  converted per provider, so a mid-conversation fallback to a different
  provider can replay the same history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, cast

import anthropic
import httpx
import openai
from google import genai
from google.genai import types as genai_types

from app.llm.contracts import (
    ErrorClassification,
    LlmBudgetExceededError,
    LlmCostOverrunError,
    LlmGovernanceError,
    LlmGovernanceUnavailableError,
    LlmPolicyDeniedError,
    LlmRequestContext,
    LlmRunInactiveError,
    ProviderErrorDisposition,
    ProviderName,
    ProviderPolicy,
    canonical_prompt_bytes,
    classify_provider_error,
    conservative_input_token_bound,
    prompt_sha256,
)
from app.store.llm_governance import (
    begin_llm_call,
    block_provider_attempt_before_egress,
    finish_llm_call,
    finish_provider_attempt,
    load_project_llm_policy,
    mark_provider_egress,
    prepare_provider_attempt,
)

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

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_PROVIDERS = ("openai", "anthropic", "google", "local")


@dataclass(frozen=True)
class ProviderRuntimeConfiguration:
    """One provider's exact endpoint and tier models from process environment."""

    provider: str
    endpoint_url: str
    fast_model: str
    reasoning_model: str


def _normalized_endpoint_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    try:
        parsed = httpx.URL(raw)
    except Exception as exc:
        raise ValueError("LLM provider endpoint is not a valid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise ValueError("LLM provider endpoint must be an absolute HTTP(S) URL")
    if parsed.userinfo:
        raise ValueError("LLM provider endpoint must not contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError(
            "LLM provider endpoint must not contain query or fragment data"
        )
    return str(parsed).rstrip("/")


def _provider_endpoint_url(provider: str) -> str:
    if provider == "openai":
        value = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    elif provider == "anthropic":
        value = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    elif provider == "google":
        value = "https://generativelanguage.googleapis.com"
    elif provider == "local":
        value = os.getenv("LOCAL_LLM_URL", "")
    else:
        raise ValueError(f"Unknown provider {provider!r}")
    return _normalized_endpoint_url(value)


def provider_runtime_configuration(provider: str) -> ProviderRuntimeConfiguration:
    """Resolve the exact router configuration without returning credentials."""
    if provider not in _PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r}")
    if not _provider_available(provider):
        credential = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
            "local": "LOCAL_LLM_URL",
        }[provider]
        raise ValueError(f"{credential} is required for provider {provider}")

    if provider == "openai":
        fast_model = os.getenv("LLM_FAST_PRIMARY", "gpt-5.4-nano")
        reasoning_model = os.getenv("LLM_REASONING_PRIMARY", "gpt-5.4-mini")
    elif provider == "anthropic":
        fast_model = os.getenv("LLM_FAST_FALLBACK", "claude-haiku-4-5-20251001")
        reasoning_model = os.getenv("LLM_REASONING_FALLBACK", "claude-sonnet-4-6")
    elif provider == "google":
        fast_model = os.getenv("LLM_FAST_GOOGLE", "gemini-2.5-flash-lite")
        reasoning_model = os.getenv("LLM_REASONING_GOOGLE", "gemini-2.5-flash")
    else:
        fast_model = reasoning_model = os.getenv("LOCAL_LLM_MODEL", "gemma4")

    fast_model = fast_model.strip()
    reasoning_model = reasoning_model.strip()
    if _MODEL_ID.fullmatch(fast_model) is None:
        raise ValueError("Configured fast model must be a valid model identifier")
    if _MODEL_ID.fullmatch(reasoning_model) is None:
        raise ValueError("Configured reasoning model must be a valid model identifier")
    return ProviderRuntimeConfiguration(
        provider=provider,
        endpoint_url=_provider_endpoint_url(provider),
        fast_model=fast_model,
        reasoning_model=reasoning_model,
    )


def _get_openai() -> openai.AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY", ""),
            base_url=_provider_endpoint_url("openai"),
            timeout=_REQUEST_TIMEOUT,
        )
    return _openai_client


def _get_anthropic() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            base_url=_provider_endpoint_url("anthropic"),
            timeout=_REQUEST_TIMEOUT,
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
            base_url=_provider_endpoint_url("local"),
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
    """Return ordered provider/model/endpoint candidates for the tier.

    Providers whose API key is not set are skipped (except local, which
    is always available when LOCAL_LLM_URL is configured).
    """
    result: list[dict[str, str]] = []
    for provider in _PROVIDERS:
        if _provider_available(provider):
            try:
                configuration = provider_runtime_configuration(provider)
            except ValueError:
                logger.error(
                    "Ignoring %s LLM candidate with invalid configuration", provider
                )
                continue
            result.append(
                {
                    "provider": provider,
                    "model": (
                        configuration.fast_model
                        if tier == "fast"
                        else configuration.reasoning_model
                    ),
                    "endpoint_url": configuration.endpoint_url,
                }
            )
    return result


def _provider_available(provider: str) -> bool:
    if provider == "openai":
        return bool(os.getenv("OPENAI_API_KEY", "").strip())
    if provider == "anthropic":
        return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    if provider == "google":
        return bool(os.getenv("GOOGLE_API_KEY", "").strip())
    if provider == "local":
        # Candidate only when its endpoint is explicitly configured. Project
        # policy still authorizes the exact local model before any request.
        return bool(os.getenv("LOCAL_LLM_URL", "").strip())
    return False


# ---------------------------------------------------------------------------
# Provider-specific completion functions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextCompletion:
    """Provider text plus provider-reported usage, when available."""

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


def _usage_value(usage: Any, *names: str) -> int | None:
    for name in names:
        candidate = getattr(usage, name, None)
        if isinstance(candidate, int) and candidate >= 0:
            return candidate
    return None


def _usage_tokens(usage: Any) -> tuple[int | None, int | None]:
    if usage is None:
        return None, None

    return (
        _usage_value(usage, "input_tokens", "prompt_tokens", "prompt_token_count"),
        _usage_value(
            usage, "output_tokens", "completion_tokens", "candidates_token_count"
        ),
    )


def _anthropic_usage_tokens(usage: Any) -> tuple[int | None, int | None]:
    if usage is None:
        return None, None
    ordinary_input = _usage_value(usage, "input_tokens")
    cache_creation = _usage_value(usage, "cache_creation_input_tokens") or 0
    cache_read = _usage_value(usage, "cache_read_input_tokens") or 0
    input_tokens = (
        ordinary_input + cache_creation + cache_read
        if ordinary_input is not None
        else None
    )
    return input_tokens, _usage_value(usage, "output_tokens")


def _google_usage_tokens(usage: Any) -> tuple[int | None, int | None]:
    if usage is None:
        return None, None
    prompt = _usage_value(usage, "prompt_token_count")
    tool_prompt = _usage_value(usage, "tool_use_prompt_token_count") or 0
    candidates = _usage_value(usage, "candidates_token_count")
    thoughts = _usage_value(usage, "thoughts_token_count") or 0
    input_tokens = prompt + tool_prompt if prompt is not None else None
    output_tokens = candidates + thoughts if candidates is not None else None
    return input_tokens, output_tokens


def _is_openai_reasoning_model(model: str) -> bool:
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


async def _openai_completion(
    model: str,
    messages: list[dict[str, str]],
    **kwargs: Any,
) -> TextCompletion:
    client = _get_openai()
    if _is_openai_reasoning_model(model):
        # Reasoning-family models 400 on `max_tokens` (they take
        # `max_completion_tokens`) and on any non-default temperature — the
        # tier defaults would otherwise make this rung fail on every call.
        if "max_tokens" in kwargs:
            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
        kwargs.pop("temperature", None)
    resp = await client.chat.completions.create(
        model=model, messages=messages, **kwargs
    )
    input_tokens, output_tokens = _usage_tokens(getattr(resp, "usage", None))
    return TextCompletion(
        text=resp.choices[0].message.content or "",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def _anthropic_completion(
    model: str,
    messages: list[dict[str, str]],
    **kwargs: Any,
) -> TextCompletion:
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
    input_tokens, output_tokens = _anthropic_usage_tokens(getattr(resp, "usage", None))
    return TextCompletion(
        text="\n".join(parts),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def _google_completion(
    model: str,
    messages: list[dict[str, str]],
    **kwargs: Any,
) -> TextCompletion:
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
    input_tokens, output_tokens = _google_usage_tokens(
        getattr(resp, "usage_metadata", None)
    )
    return TextCompletion(
        text=resp.text or "",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def _local_completion(
    model: str,
    messages: list[dict[str, str]],
    **kwargs: Any,
) -> TextCompletion:
    client = _get_local()
    resp = await client.chat.completions.create(
        model=model, messages=messages, **kwargs
    )
    input_tokens, output_tokens = _usage_tokens(getattr(resp, "usage", None))
    return TextCompletion(
        text=resp.choices[0].message.content or "",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


_PROVIDER_FN = {
    "openai": _openai_completion,
    "anthropic": _anthropic_completion,
    "google": _google_completion,
    "local": _local_completion,
}


class _AuditedCompletion(Protocol):
    input_tokens: int | None
    output_tokens: int | None


_CompletionT = TypeVar("_CompletionT", bound=_AuditedCompletion)


def _governance_error_classification(
    exc: LlmGovernanceError,
) -> ErrorClassification:
    if isinstance(exc, LlmBudgetExceededError):
        return "budget_exceeded"
    if isinstance(exc, LlmRunInactiveError):
        return "run_inactive"
    if isinstance(exc, LlmPolicyDeniedError):
        return "policy_denied"
    if isinstance(exc, LlmCostOverrunError):
        return "cost_overrun"
    if isinstance(exc, LlmGovernanceUnavailableError):
        return "governance_unavailable"
    return "unknown"


async def _route_with_fallback(
    model_tier: str,
    *,
    context: LlmRequestContext,
    prompt_bytes: bytes,
    operation: str,
    invoke: Callable[[str, str, dict[str, Any]], Awaitable[_CompletionT]],
    kwargs: dict[str, Any],
) -> _CompletionT:
    """Run one completion through policy, budget, audit, and safe fallback."""
    if model_tier not in _TIER_DEFAULTS:
        raise ValueError(
            f"Unknown model_tier {model_tier!r} — expected one of "
            f"{sorted(_TIER_DEFAULTS)}"
        )

    merged = {**_TIER_DEFAULTS[model_tier], **kwargs}
    max_output_tokens = merged["max_tokens"]
    if (
        not isinstance(max_output_tokens, int)
        or isinstance(max_output_tokens, bool)
        or max_output_tokens <= 0
    ):
        raise ValueError("max_tokens must be a positive integer")
    prompt_hash = prompt_sha256(prompt_bytes)
    estimated_input_tokens = conservative_input_token_bound(prompt_bytes)

    policy = await load_project_llm_policy(context)
    call_id = await begin_llm_call(context, prompt_sha256=prompt_hash)
    models = _tier_models(model_tier)
    eligible: list[tuple[dict[str, str], ProviderPolicy]] = []
    for entry in models:
        provider_policy = policy.provider_policy(
            context,
            entry["provider"],
            entry["model"],
            entry["endpoint_url"],
        )
        if provider_policy is not None:
            eligible.append((entry, provider_policy))

    if not models:
        message = (
            f"No LLM providers are configured for tier {model_tier!r}; set a "
            "provider credential or LOCAL_LLM_URL"
        )
        await finish_llm_call(
            context,
            call_id=call_id,
            status="blocked",
            error_classification="no_provider",
            error_message=message,
        )
        raise RuntimeError(message)
    if not eligible:
        message = (
            f"Project policy permits none of the configured {model_tier!r} "
            f"provider/models for {context.data_classification} data"
        )
        await finish_llm_call(
            context,
            call_id=call_id,
            status="blocked",
            error_classification="policy_denied",
            error_message=message,
        )
        raise RuntimeError(message)

    last_exc: Exception | None = None
    last_disposition: ProviderErrorDisposition | None = None
    last_provider: str | None = None
    attempts = 0
    for entry, _ in eligible:
        provider = cast(ProviderName, entry["provider"])
        model = entry["model"]
        endpoint_url = entry["endpoint_url"]
        if (
            last_provider is not None
            and provider != last_provider
            and not policy.allow_cross_vendor_retry
        ):
            logger.warning(
                "LLM %s stopped before cross-vendor retry (%s -> %s)",
                operation,
                last_provider,
                provider,
            )
            break

        attempts += 1
        try:
            prepared = await prepare_provider_attempt(
                context,
                call_id=call_id,
                attempt_number=attempts,
                provider=provider,
                model=model,
                endpoint_url=endpoint_url,
                prompt_sha256=prompt_hash,
                estimated_input_tokens=estimated_input_tokens,
                max_output_tokens=max_output_tokens,
            )
        except LlmGovernanceError as exc:
            classification = _governance_error_classification(exc)
            await finish_llm_call(
                context,
                call_id=call_id,
                status="blocked",
                error_classification=classification,
                error_message=str(exc),
            )
            raise

        try:
            await mark_provider_egress(context, attempt_id=prepared.attempt_id)
        except asyncio.CancelledError:
            await block_provider_attempt_before_egress(
                context,
                attempt_id=prepared.attempt_id,
                error_classification="cancelled",
                error_message="LLM request task was cancelled before egress",
            )
            await finish_llm_call(
                context,
                call_id=call_id,
                status="cancelled",
                error_classification="cancelled",
                error_message="LLM request task was cancelled before egress",
            )
            raise
        except LlmGovernanceError as exc:
            classification = _governance_error_classification(exc)
            await block_provider_attempt_before_egress(
                context,
                attempt_id=prepared.attempt_id,
                error_classification=classification,
                error_message=str(exc),
            )
            await finish_llm_call(
                context,
                call_id=call_id,
                status="blocked",
                error_classification=classification,
                error_message=str(exc),
            )
            raise
        started = time.monotonic()
        try:
            result = await invoke(provider, model, dict(merged))
        except asyncio.CancelledError:
            latency_ms = int((time.monotonic() - started) * 1000)
            await finish_provider_attempt(
                context,
                attempt=prepared,
                status="cancelled",
                latency_ms=latency_ms,
                input_tokens=None,
                output_tokens=None,
                error_classification="cancelled",
                error_message="LLM request task was cancelled after egress",
            )
            await finish_llm_call(
                context,
                call_id=call_id,
                status="cancelled",
                error_classification="cancelled",
                error_message="LLM request task was cancelled after egress",
            )
            raise
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            disposition = classify_provider_error(exc)
            # Provider exception strings can echo request fragments. Persist
            # and log only the type plus canonical classification.
            error_message = f"{type(exc).__name__} ({disposition.classification})"
            await finish_provider_attempt(
                context,
                attempt=prepared,
                status="failed",
                latency_ms=latency_ms,
                input_tokens=None,
                output_tokens=None,
                error_classification=disposition.classification,
                error_message=error_message,
                retryable=disposition.retryable,
            )
            logger.warning(
                "LLM %s failed (provider=%s, model=%s, classification=%s, "
                "retryable=%s, exception_type=%s)",
                operation,
                provider,
                model,
                disposition.classification,
                disposition.retryable,
                type(exc).__name__,
            )
            last_exc = exc
            last_disposition = disposition
            last_provider = provider
            if not disposition.retryable:
                await finish_llm_call(
                    context,
                    call_id=call_id,
                    status="failed",
                    error_classification=disposition.classification,
                    error_message=error_message,
                )
                raise RuntimeError(
                    f"LLM {operation} failed without a safe retry "
                    f"({provider}/{model}, {disposition.classification})"
                ) from exc
            continue

        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            await finish_provider_attempt(
                context,
                attempt=prepared,
                status="succeeded",
                latency_ms=latency_ms,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        except LlmCostOverrunError as exc:
            await finish_llm_call(
                context,
                call_id=call_id,
                status="failed",
                error_classification="cost_overrun",
                error_message=str(exc),
            )
            raise
        await finish_llm_call(context, call_id=call_id, status="succeeded")
        # Visible at info level so fallback behavior remains operationally
        # searchable in addition to the authoritative attempt ledger.
        logger.info("LLM %s ok (provider=%s, model=%s)", operation, provider, model)
        return result

    classification: ErrorClassification = (
        last_disposition.classification
        if last_disposition is not None
        else "no_provider"
    )
    message = f"No safe LLM retry remained for tier {model_tier!r}"
    await finish_llm_call(
        context,
        call_id=call_id,
        status="failed",
        error_classification=classification,
        error_message=message,
    )
    raise RuntimeError(message) from last_exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def chat_completion(
    model_tier: str,
    messages: list[dict[str, str]],
    *,
    context: LlmRequestContext,
    **kwargs: Any,
) -> str:
    """Route a chat completion through the governed provider chain.

    Every provider attempt is policy-checked, budgeted, and audited. Only
    classified retryable failures may advance to another permitted candidate.

    Args:
        model_tier: "fast" or "reasoning"
        messages: Chat messages in OpenAI format (role + content)
        context: Required tenant/run/purpose/data-classification scope.
        **kwargs: Additional parameters forwarded to the provider.

    Returns:
        The assistant's response text.

    Raises:
        RuntimeError: If policy, budget, or all safe provider candidates fail.
    """

    async def invoke(
        provider: str, model: str, provider_kwargs: dict[str, Any]
    ) -> TextCompletion:
        return await _PROVIDER_FN[provider](model, messages, **provider_kwargs)

    completion = await _route_with_fallback(
        model_tier,
        context=context,
        prompt_bytes=canonical_prompt_bytes(messages=messages, tools=None),
        operation="call",
        invoke=invoke,
        kwargs=kwargs,
    )
    return completion.text


# ---------------------------------------------------------------------------
# Tool calling (function calling)
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """One tool invocation requested by the model, provider-normalized."""

    id: str
    name: str
    arguments: dict[str, Any]
    #: Opaque Gemini reasoning signature that must be replayed with the call.
    thought_signature: bytes | None = None


@dataclass
class ToolCompletion:
    """A completion that may request tool calls instead of (or with) text."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Tolerate the model emitting arguments as a JSON string or a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in tools
    ]


def _openai_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-serialize normalized messages for the OpenAI wire (arguments as str)."""
    wire: list[dict[str, Any]] = []
    for msg in messages:
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            wire.append(
                {
                    "role": "assistant",
                    "content": msg.get("content") or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"]),
                            },
                        }
                        for tc in msg["tool_calls"]
                    ],
                }
            )
        elif msg["role"] == "tool":
            wire.append(
                {
                    "role": "tool",
                    "tool_call_id": msg["tool_call_id"],
                    "content": msg["content"],
                }
            )
        else:
            wire.append({"role": msg["role"], "content": msg.get("content") or ""})
    return wire


async def _openai_completion_tools(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    client: openai.AsyncOpenAI,
    *,
    force_text: bool = False,
    **kwargs: Any,
) -> ToolCompletion:
    if _is_openai_reasoning_model(model):
        if "max_tokens" in kwargs:
            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
        kwargs.pop("temperature", None)
    if tools:
        kwargs["tools"] = _openai_tools(tools)
        if force_text:
            kwargs["tool_choice"] = "none"
    resp = await client.chat.completions.create(
        model=model, messages=_openai_tool_messages(messages), **kwargs
    )
    message = resp.choices[0].message
    calls = [
        ToolCall(
            id=tc.id,
            name=tc.function.name,
            arguments=_parse_arguments(tc.function.arguments),
        )
        for tc in (message.tool_calls or [])
    ]
    input_tokens, output_tokens = _usage_tokens(getattr(resp, "usage", None))
    return ToolCompletion(
        text=message.content or "",
        tool_calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _anthropic_tool_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Convert normalized messages to Anthropic's (system, messages) shape.

    Consecutive tool results merge into ONE user turn — Anthropic requires
    every tool_result for an assistant turn's tool_use blocks to arrive in the
    single following user message.
    """
    system_text = ""
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg["role"]
        if role == "system":
            system_text = msg["content"]
        elif role == "assistant" and msg.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if msg.get("content"):
                blocks.append({"type": "text", "text": msg["content"]})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc["arguments"],
                }
                for tc in msg["tool_calls"]
            )
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            result_block = {
                "type": "tool_result",
                "tool_use_id": msg["tool_call_id"],
                "content": msg["content"],
            }
            if (
                out
                and out[-1]["role"] == "user"
                and isinstance(out[-1]["content"], list)
            ):
                out[-1]["content"].append(result_block)
            else:
                out.append({"role": "user", "content": [result_block]})
        else:
            out.append({"role": role, "content": msg.get("content") or ""})
    return system_text, out


async def _anthropic_completion_tools(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    client: anthropic.AsyncAnthropic,
    *,
    force_text: bool = False,
    **kwargs: Any,
) -> ToolCompletion:
    system_text, chat_messages = _anthropic_tool_messages(messages)
    max_tokens = kwargs.pop("max_tokens", 4096)
    if tools:
        kwargs["tools"] = [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in tools
        ]
        if force_text:
            kwargs["tool_choice"] = {"type": "none"}
    resp = await client.messages.create(
        model=model,
        system=system_text,
        messages=chat_messages,
        max_tokens=max_tokens,
        **kwargs,
    )
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in resp.content:
        block_type = getattr(block, "type", "")
        if block_type == "tool_use":
            calls.append(
                ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=_parse_arguments(block.input),
                )
            )
        elif getattr(block, "text", None):
            text_parts.append(block.text)
    input_tokens, output_tokens = _anthropic_usage_tokens(getattr(resp, "usage", None))
    return ToolCompletion(
        text="\n".join(text_parts),
        tool_calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _google_tool_contents(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert normalized messages to google-genai (system_instruction, contents)."""
    system_instruction: str | None = None
    contents: list[dict[str, Any]] = []
    for msg in messages:
        role = msg["role"]
        if role == "system":
            system_instruction = msg["content"]
        elif role == "assistant" and msg.get("tool_calls"):
            parts: list[dict[str, Any]] = []
            if msg.get("content"):
                parts.append({"text": msg["content"]})
            for tc in msg["tool_calls"]:
                call_part: dict[str, Any] = {
                    "function_call": {
                        "id": tc["id"],
                        "name": tc["name"],
                        "args": tc["arguments"],
                    }
                }
                if tc.get("thought_signature") is not None:
                    call_part["thought_signature"] = tc["thought_signature"]
                parts.append(call_part)
            contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            # Gemini matches responses by function NAME (it has no call ids).
            response_part = {
                "function_response": {
                    "id": msg["tool_call_id"],
                    "name": msg.get("name", ""),
                    "response": {"result": msg["content"]},
                }
            }
            if (
                contents
                and contents[-1]["role"] == "user"
                and all(
                    "function_response" in part
                    for part in contents[-1].get("parts", [])
                )
            ):
                contents[-1]["parts"].append(response_part)
            else:
                contents.append({"role": "user", "parts": [response_part]})
        else:
            gr = "model" if role == "assistant" else "user"
            contents.append({"role": gr, "parts": [{"text": msg.get("content") or ""}]})
    return system_instruction, contents


async def _google_completion_tools(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    client: genai.Client,
    *,
    force_text: bool = False,
    **kwargs: Any,
) -> ToolCompletion:
    system_instruction, contents = _google_tool_contents(messages)
    genai_tools = None
    if tools:
        genai_tools = [
            genai_types.Tool(
                function_declarations=[
                    genai_types.FunctionDeclaration(
                        name=t["name"],
                        description=t["description"],
                        # Raw JSON schema passthrough — genai's typed Schema
                        # can't express pydantic's $defs/$ref output.
                        parameters_json_schema=t["parameters"],
                    )
                    for t in tools
                ]
            )
        ]
    config = genai_types.GenerateContentConfig(
        max_output_tokens=kwargs.get("max_tokens", 4096),
        temperature=kwargs.get("temperature"),
        system_instruction=system_instruction,
        tools=genai_tools,
        tool_config=(
            genai_types.ToolConfig(
                function_calling_config=genai_types.FunctionCallingConfig(
                    mode=genai_types.FunctionCallingConfigMode.NONE
                )
            )
            if tools and force_text
            else None
        ),
    )
    resp = await client.aio.models.generate_content(
        model=model, contents=contents, config=config
    )
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    candidates = resp.candidates or []
    parts = candidates[0].content.parts if candidates and candidates[0].content else []
    for index, part in enumerate(parts or []):
        fc = getattr(part, "function_call", None)
        if fc is not None:
            calls.append(
                ToolCall(
                    # Older Gemini models may omit ids. Preserve a real id
                    # whenever present and synthesize only as a compatibility
                    # fallback for the normalized cross-provider transcript.
                    id=getattr(fc, "id", None) or f"call_{index}_{fc.name}",
                    name=fc.name or "",
                    arguments=_parse_arguments(dict(fc.args or {})),
                    thought_signature=getattr(part, "thought_signature", None),
                )
            )
        elif getattr(part, "text", None):
            text_parts.append(part.text)
    input_tokens, output_tokens = _google_usage_tokens(
        getattr(resp, "usage_metadata", None)
    )
    return ToolCompletion(
        text="\n".join(text_parts),
        tool_calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def chat_completion_with_tools(
    model_tier: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    context: LlmRequestContext,
    force_text: bool = False,
    **kwargs: Any,
) -> ToolCompletion:
    """Route a tool-enabled completion through the governed provider chain.

    Args:
        model_tier: "fast" or "reasoning".
        messages: Normalized OpenAI-shape messages. Assistant messages may
            carry ``tool_calls`` (list of ``{"id", "name", "arguments": dict}``);
            tool results are ``{"role": "tool", "tool_call_id", "name", "content"}``.
        tools: Neutral tool specs ``{"name", "description", "parameters"}``
            (parameters = JSON schema). ``None``/empty sends no tools.
        context: Required tenant/run/purpose/data-classification scope.
        force_text: Keep tool declarations available for validating historical
            tool calls, but forbid the provider from requesting a new call.

    Returns:
        A :class:`ToolCompletion` with the assistant text and any tool calls.

    Raises:
        RuntimeError: If policy, budget, or all safe provider candidates fail.
    """

    async def invoke(
        provider: str, model: str, provider_kwargs: dict[str, Any]
    ) -> ToolCompletion:
        if provider == "openai":
            client = _get_openai()
        elif provider == "anthropic":
            client = _get_anthropic()
        elif provider == "google":
            client = _get_google()
        else:  # local — OpenAI-compatible servers speak the tools dialect
            client = _get_local()

        if provider == "anthropic":
            return await _anthropic_completion_tools(
                model,
                messages,
                tools,
                client,
                force_text=force_text,
                **provider_kwargs,
            )
        if provider == "google":
            return await _google_completion_tools(
                model,
                messages,
                tools,
                client,
                force_text=force_text,
                **provider_kwargs,
            )
        return await _openai_completion_tools(
            model,
            messages,
            tools,
            client,
            force_text=force_text,
            **provider_kwargs,
        )

    return await _route_with_fallback(
        model_tier,
        context=context,
        prompt_bytes=canonical_prompt_bytes(messages=messages, tools=tools),
        operation="tool call",
        invoke=invoke,
        kwargs=kwargs,
    )
