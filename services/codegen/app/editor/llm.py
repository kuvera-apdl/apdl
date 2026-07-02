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
from collections.abc import Awaitable, Callable

from app import config

logger = logging.getLogger(__name__)

#: ``(system, user) -> completion text``; ``None`` means the call failed and the
#: caller should proceed without it (fail-open — see module docstring).
CompleteFn = Callable[[str, str], Awaitable[str | None]]


def resolve_completer(
    model: str | None = None, timeout: float | None = None
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

    try:
        # Same key resolution LiteLLM applies on the real call; unknown model
        # mappings raise, in which case we try the call anyway and let it speak.
        if not litellm.validate_environment(model).get("keys_in_environment", False):
            logger.warning(
                "No provider key in env for helper model %s; "
                "auxiliary LLM steps are disabled.",
                model,
            )
            return None
    except Exception:  # pragma: no cover - depends on litellm internals
        pass

    async def complete(system: str, user: str) -> str | None:
        try:
            response = await litellm.acompletion(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                timeout=timeout,
                # Newer models reject sampler params (temperature); never send any.
                drop_params=True,
            )
            content = response["choices"][0]["message"]["content"]
            return content.strip() if isinstance(content, str) else None
        except Exception as exc:
            # An auxiliary call must never sink the changeset; the caller
            # proceeds without it.
            logger.warning("Auxiliary LLM call (%s) failed: %s", model, exc)
            return None

    return complete
