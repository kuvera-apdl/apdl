"""In-process LLM completion for codegen's auxiliary calls (brief + review).

The editing engine (Aider) talks to the model on its own; this module covers the
two *auxiliary* calls around it — compiling the task spec into a repo-grounded
engineering brief before the edit, and reviewing the produced diff against the
spec after it. It reuses Aider's LiteLLM dependency so both calls stay
model-agnostic (the same ``CODEGEN_MODEL`` id space) without adding a provider
SDK to the service.

LiteLLM ships with the optional ``agent`` extra (``aider-chat``). Where it is
absent — unit tests, a FakeEditor deployment — :func:`resolve_completer` returns
``None`` and the callers skip their step; the auxiliary calls are quality
amplifiers, never a reason a changeset cannot run at all.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping

from app import config

logger = logging.getLogger(__name__)

#: ``(system, user) -> completion text``; ``None`` means the call failed and the
#: caller should proceed without it (fail-open — see module docstring).
CompleteFn = Callable[[str, str], Awaitable[str | None]]


def resolve_completer(
    model: str | None = None,
    timeout: float | None = None,
    *,
    api_key: str | None = None,
    endpoint_url: str | None = None,
    raise_errors: bool = False,
    usage_callback: Callable[[int, int], None] | None = None,
) -> CompleteFn | None:
    """Build a completion function for the configured helper model.

    Returns ``None`` when the call path cannot work — LiteLLM not installed, or
    no provider key for the model in the environment — so callers can skip their
    step instead of failing the changeset on a doomed request.
    """
    model = model or config.codegen_helper_model()
    timeout = timeout if timeout is not None else config.codegen_llm_timeout()

    try:
        import litellm
    except ImportError:
        logger.info("LiteLLM is not installed; auxiliary LLM steps are disabled.")
        return None

    if api_key is None:
        try:
            # This branch belongs only to an explicit trusted-local/custom
            # editor; tenant execution supplies a phase-scoped key directly.
            if not litellm.validate_environment(model).get(
                "keys_in_environment", False
            ):
                logger.warning(
                    "No explicit local credential for helper model %s; "
                    "auxiliary LLM steps are disabled.",
                    model,
                )
                return None
        except Exception:  # pragma: no cover - depends on litellm internals
            pass

    async def complete(system: str, user: str) -> str | None:
        try:
            call_options = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "timeout": timeout,
                "drop_params": True,
            }
            if api_key is not None:
                call_options["api_key"] = api_key
            if endpoint_url is not None:
                call_options["api_base"] = endpoint_url
            response = await litellm.acompletion(
                **call_options,
            )
            usage = (
                response.get("usage")
                if isinstance(response, Mapping)
                else getattr(response, "usage", None)
            )
            usage_get = getattr(usage, "get", None)
            if usage_callback is not None and callable(usage_get):
                input_tokens = usage_get("prompt_tokens")
                output_tokens = usage_get("completion_tokens")
                if (
                    isinstance(input_tokens, int)
                    and not isinstance(input_tokens, bool)
                    and 0 <= input_tokens <= 10_000_000_000
                    and isinstance(output_tokens, int)
                    and not isinstance(output_tokens, bool)
                    and 0 <= output_tokens <= 10_000_000_000
                ):
                    usage_callback(input_tokens, output_tokens)
            content = response["choices"][0]["message"]["content"]
            return content.strip() if isinstance(content, str) else None
        except Exception as exc:
            # An auxiliary call must never sink the changeset; the caller
            # proceeds without it.
            logger.warning(
                "Auxiliary LLM call (%s) failed with %s",
                model,
                type(exc).__name__,
            )
            if raise_errors:
                raise
            return None

    return complete
