"""Multi-provider LLM router with fallback — OpenAI, Anthropic, Google, and local models.

Tier 1 (fast): High-throughput, lower-cost tasks — summarisation, classification,
               UI config generation.
Tier 2 (reasoning): Complex analysis, experiment design, feature proposals.

Each tier tries providers in order and falls back on failure.

Two entry points share the tier/fallback machinery:

* :func:`chat_completion` — plain text in/out (the original API).
* :func:`chat_completion_with_tools` — function calling. Conversations use the
  OpenAI wire shape as the neutral format (assistant messages may carry
  ``tool_calls``; tool results are ``{"role": "tool", ...}`` messages) and are
  converted per provider, so a mid-conversation fallback to a different
  provider can replay the same history.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

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

_CompletionT = TypeVar("_CompletionT")


async def _route_with_fallback(
    model_tier: str,
    *,
    operation: str,
    invoke: Callable[[str, str, dict[str, Any]], Awaitable[_CompletionT]],
    kwargs: dict[str, Any],
) -> _CompletionT:
    """Run one completion shape through the shared provider fallback chain."""
    if model_tier not in _TIER_DEFAULTS:
        raise ValueError(
            f"Unknown model_tier {model_tier!r} — expected one of "
            f"{sorted(_TIER_DEFAULTS)}"
        )

    models = _tier_models(model_tier)
    if not models:
        raise RuntimeError(
            f"No LLM providers configured for tier '{model_tier}'. "
            "Set OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY, or "
            "LOCAL_LLM_URL."
        )

    merged = {**_TIER_DEFAULTS[model_tier], **kwargs}
    last_exc: Exception | None = None
    for entry in models:
        provider = entry["provider"]
        model = entry["model"]
        try:
            result = await invoke(provider, model, dict(merged))
            # Visible at info level so a permanently-failing primary (every
            # call silently landing on a fallback rung) is observable.
            logger.info(
                "LLM %s ok (provider=%s, model=%s)", operation, provider, model
            )
            return result
        except Exception as exc:
            logger.warning(
                "LLM %s failed (provider=%s, model=%s): %s",
                operation,
                provider,
                model,
                exc,
            )
            last_exc = exc

    raise RuntimeError(
        f"All LLM providers failed for tier '{model_tier}'"
    ) from last_exc


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
    async def invoke(
        provider: str, model: str, provider_kwargs: dict[str, Any]
    ) -> str:
        return await _PROVIDER_FN[provider](model, messages, **provider_kwargs)

    return await _route_with_fallback(
        model_tier,
        operation="call",
        invoke=invoke,
        kwargs=kwargs,
    )


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
    return ToolCompletion(text=message.content or "", tool_calls=calls)


def _anthropic_tool_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
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
                {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["arguments"]}
                for tc in msg["tool_calls"]
            )
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            result_block = {
                "type": "tool_result",
                "tool_use_id": msg["tool_call_id"],
                "content": msg["content"],
            }
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
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
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
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
    return ToolCompletion(text="\n".join(text_parts), tool_calls=calls)


def _google_tool_contents(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
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
    return ToolCompletion(text="\n".join(text_parts), tool_calls=calls)


async def chat_completion_with_tools(
    model_tier: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    force_text: bool = False,
    **kwargs: Any,
) -> ToolCompletion:
    """Route a tool-enabled chat completion through the provider chain.

    Args:
        model_tier: "fast" or "reasoning".
        messages: Normalized OpenAI-shape messages. Assistant messages may
            carry ``tool_calls`` (list of ``{"id", "name", "arguments": dict}``);
            tool results are ``{"role": "tool", "tool_call_id", "name", "content"}``.
        tools: Neutral tool specs ``{"name", "description", "parameters"}``
            (parameters = JSON schema). ``None``/empty sends no tools.
        force_text: Keep tool declarations available for validating historical
            tool calls, but forbid the provider from requesting a new call.

    Returns:
        A :class:`ToolCompletion` with the assistant text and any tool calls.

    Raises:
        RuntimeError: If all providers fail.
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
        operation="tool call",
        invoke=invoke,
        kwargs=kwargs,
    )
