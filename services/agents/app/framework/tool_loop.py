"""Bounded agentic tool loop — lets the reasoning model drive catalog tools.

This is the piece that turns an agent from "one prompt over pre-gathered
data" into an investigator: the model sees the tool schemas, requests calls,
gets the (truncated) results back, and iterates until it produces a final
text answer or exhausts the step budget.

Safety properties, in order of importance:

* Only tools in the current agent's declared schema allow-list are dispatched,
  and every allowed name must also resolve through
  :data:`app.framework.tool_catalog.TOOL_CATALOG`. ``run_tool`` validates
  params per call and injects ``project_id`` plus the context's date window,
  so the model can never widen its scope.
* ``max_steps`` bounds the number of tool ROUNDS (one model turn may request
  several calls). On exhaustion the model is forced to answer with further
  tool calls disabled — the loop cannot run away.
* Every tool call is audit-logged (``{agent}_tool_call``) so the console can
  show the investigation trace, and optionally collected into ``trace`` for
  the wizard's dry-run preview.
* Tool results are truncated before re-entering the prompt so one fat
  retention grid cannot blow the context window.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.framework.context import AgentContext
from app.llm.router import chat_completion_with_tools

logger = logging.getLogger(__name__)

#: Per-result character cap before a tool result re-enters the conversation.
RESULT_CHAR_CAP = 8_000
#: Hard ceiling on tool calls within one round — a model that requests dozens
#: of parallel calls in a single turn is misbehaving, not thorough.
MAX_CALLS_PER_ROUND = 8


@dataclass
class ToolTraceEntry:
    """One executed tool call, for audit/preview surfaces."""

    tool: str
    params: dict[str, Any]
    result: str | None = None
    error: str | None = None
    elapsed_ms: int = 0


@dataclass
class ToolLoopResult:
    """Final text plus the executed-call trace."""

    text: str
    trace: list[ToolTraceEntry] = field(default_factory=list)
    rounds: int = 0


def _truncate(blob: str, cap: int = RESULT_CHAR_CAP) -> str:
    if len(blob) <= cap:
        return blob
    return blob[:cap] + f'... [truncated {len(blob) - cap} of {len(blob)} chars]'


async def _execute_call(
    ctx: AgentContext,
    name: str,
    arguments: dict[str, Any],
    *,
    allowed_tools: frozenset[str],
) -> ToolTraceEntry:
    """Run one catalog tool; failures become result content, never raises.

    The model must see its own mistakes (unknown tool, bad params, query
    service down) as tool output so it can correct course — an exception here
    would kill the whole agent over one bad call.
    """
    started = time.monotonic()
    if name not in allowed_tools:
        error = f"PermissionError: Tool {name!r} is not enabled for this agent"
        logger.warning("Rejected out-of-scope tool %s", name)
        return ToolTraceEntry(
            tool=name,
            params=arguments,
            error=error,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    # Late import so tests monkeypatching app.framework.tool_catalog.run_tool
    # are honored (same convention as CustomAgent.gather used).
    from app.framework import tool_catalog

    try:
        output = await tool_catalog.run_tool(ctx, name, arguments)
        result = _truncate(json.dumps(output, default=str))
        return ToolTraceEntry(
            tool=name,
            params=arguments,
            result=result,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        logger.warning("Tool %s failed in loop: %s", name, exc)
        return ToolTraceEntry(
            tool=name,
            params=arguments,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


async def run_preset_tools(
    ctx: AgentContext,
    *,
    agent_name: str,
    preset_tools: list[dict[str, Any]],
    log_tool_calls: bool = True,
) -> list[ToolTraceEntry]:
    """Execute author-preset tool calls verbatim, before the reasoning step.

    The deterministic counterpart to :func:`run_tool_loop`: the agent's
    definition fixes both the tool and its parameters, so the same calls run
    on every invocation and their results are handed to the model up front.
    Same safety boundary (catalog-only, ctx-scoped ``run_tool``), same
    failure containment (an error becomes the entry's content — the agent
    still reasons over whatever succeeded), same audit shape (``round`` 0
    marks an entry as preset in the trace the console renders).
    """
    trace: list[ToolTraceEntry] = []
    allowed_tools = frozenset(entry["tool"] for entry in preset_tools)
    for entry in preset_tools:
        executed = await _execute_call(
            ctx,
            entry["tool"],
            entry.get("params") or {},
            allowed_tools=allowed_tools,
        )
        trace.append(executed)
        if log_tool_calls:
            await ctx.audit.log(
                ctx.run_id,
                f"{agent_name}_tool_call",
                {
                    "tool": executed.tool,
                    "params": executed.params,
                    "error": executed.error,
                    "result_chars": len(executed.result or ""),
                    "elapsed_ms": executed.elapsed_ms,
                    "round": 0,
                    "preset": True,
                },
            )
    return trace


async def run_tool_loop(
    ctx: AgentContext,
    *,
    agent_name: str,
    system_prompt: str,
    user_prompt: str,
    tool_schemas: list[dict[str, Any]],
    model_tier: str = "reasoning",
    max_steps: int = 8,
    log_tool_calls: bool = True,
    terminal_result_for_tool: Callable[[ToolTraceEntry], str | None] | None = None,
) -> ToolLoopResult:
    """Run the model with tools until it answers in text or the budget ends.

    Args:
        ctx: Agent context (scopes every tool call; carries the audit logger).
        agent_name: For audit entries and logs.
        system_prompt / user_prompt: The agent's reasoning prompt.
        tool_schemas: Neutral specs from :func:`tool_catalog.llm_tool_schemas`.
        model_tier: "fast" or "reasoning".
        max_steps: Max tool ROUNDS before the final answer is forced.
        log_tool_calls: Write audit entries per call (off for dry-run testing,
            which must stay side-effect free).
        terminal_result_for_tool: Optional deterministic stop hook. Returning
            text after a tool result ends the loop without another LLM call.

    Returns:
        The final assistant text plus the executed tool-call trace.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    trace: list[ToolTraceEntry] = []
    allowed_tools = frozenset(schema["name"] for schema in tool_schemas)

    for round_index in range(max_steps):
        completion = await chat_completion_with_tools(
            model_tier=model_tier, messages=messages, tools=tool_schemas
        )
        if not completion.tool_calls:
            return ToolLoopResult(text=completion.text, trace=trace, rounds=round_index)

        requested = completion.tool_calls[:MAX_CALLS_PER_ROUND]
        dropped = len(completion.tool_calls) - len(requested)
        if dropped:
            logger.warning(
                "[%s] Model requested %d tool calls in one round; running only %d",
                agent_name, len(completion.tool_calls), MAX_CALLS_PER_ROUND,
            )

        messages.append(
            {
                "role": "assistant",
                "content": completion.text,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                        "thought_signature": tc.thought_signature,
                    }
                    for tc in requested
                ],
            }
        )

        # Sequential on purpose: rounds are small (models typically request
        # 1–3 calls) and the query service is a shared dependency — the old
        # plan-executor's burst concurrency is what this loop replaces.
        for tc in requested:
            entry = await _execute_call(
                ctx,
                tc.name,
                tc.arguments,
                allowed_tools=allowed_tools,
            )
            trace.append(entry)
            if log_tool_calls:
                await ctx.audit.log(
                    ctx.run_id,
                    f"{agent_name}_tool_call",
                    {
                        "tool": entry.tool,
                        "params": entry.params,
                        "error": entry.error,
                        "result_chars": len(entry.result or ""),
                        "elapsed_ms": entry.elapsed_ms,
                        "round": round_index + 1,
                    },
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": entry.result
                    if entry.error is None
                    else json.dumps({"error": entry.error}),
                }
            )
            if terminal_result_for_tool is not None:
                terminal_text = terminal_result_for_tool(entry)
                if terminal_text is not None:
                    logger.info(
                        "[%s] Tool %s produced a deterministic terminal result",
                        agent_name,
                        entry.tool,
                    )
                    return ToolLoopResult(
                        text=terminal_text,
                        trace=trace,
                        rounds=round_index + 1,
                    )

    # Budget exhausted: force a final answer. Tool declarations stay present
    # because providers validate historical tool calls against them; the
    # explicit force_text control prevents any new calls.
    messages.append(
        {
            "role": "user",
            "content": (
                "You have used your entire tool budget. Produce your final answer "
                "now from the results you already have, in the required format."
            ),
        }
    )
    completion = await chat_completion_with_tools(
        model_tier=model_tier,
        messages=messages,
        tools=tool_schemas,
        force_text=True,
    )
    logger.info(
        "[%s] Tool loop hit max_steps=%d (%d calls executed)",
        agent_name, max_steps, len(trace),
    )
    return ToolLoopResult(text=completion.text, trace=trace, rounds=max_steps)
