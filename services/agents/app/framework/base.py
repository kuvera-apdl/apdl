"""BaseAgent — the Template Method that every APDL agent follows.

Every agent in this codebase runs the same lifecycle:

    retrieve_context  ->  gather  ->  build_prompt  ->  reason  ->  act  ->  persist
    (vector memory)       (tools)     (LLM input)      (LLM)       (deploy) (memory)

``BaseAgent`` implements that skeleton once in :meth:`run` and exposes the
varying parts as overridable hooks. Cross-cutting concerns — memory retrieval
and persistence, JSON parsing, error isolation — live in the base class so a
new agent is a small subclass plus a prompt module, not 200 lines of
copy-pasted graph wiring.

Declarative class attributes configure the invariant parts; hook methods
supply the behaviour:

    class MyAgent(BaseAgent):
        name = "my_agent"
        produces = "my_outputs"
        system_prompt = MY_SYSTEM
        memory_query = "things my agent should recall"

        async def gather(self, ctx, state):
            return {"data": await some_tool(ctx.project_id)}

        def build_prompt(self, ctx, state, working):
            return MY_PROMPT.format(data=working["data"], context=working["context"])
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from app.framework.context import AgentContext, AgentResult, MemoryEntry
from app.llm.router import chat_completion
from app.llm.utils import parse_llm_json

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """Template Method base class for all agents."""

    # --- declarative configuration (subclasses set these) -------------------
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: Pipeline ordering hint; lower runs earlier in the supervisor.
    order: ClassVar[int] = 100
    #: System prompt for the primary reasoning call.
    system_prompt: ClassVar[str] = ""
    #: LLM tier for the primary reasoning call ("fast" or "reasoning").
    model_tier: ClassVar[str] = "reasoning"
    #: Natural-language query used to retrieve relevant long-term memory.
    memory_query: ClassVar[str | None] = None
    memory_top_k: ClassVar[int] = 5
    #: State keys this agent needs present (and truthy) before it can run.
    requires: ClassVar[tuple[str, ...]] = ()
    #: State key under which this agent's output is stored for downstream agents.
    produces: ClassVar[str] = "output"
    #: Shape of the parsed LLM output — "object" or "list".
    parse_as: ClassVar[str] = "object"

    # --- lifecycle hooks (override as needed) -------------------------------

    async def gather(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> dict[str, Any]:
        """Collect tool / warehouse data. Returns a dict merged into ``working``.

        ``working`` already carries the original state plus ``context``
        (retrieved memory), so a gather step may condition its tool calls or an
        intermediate LLM call on what was recalled.
        """
        return {}

    @abstractmethod
    def build_prompt(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> str | None:
        """Build the user prompt for the reasoning call.

        ``working`` holds the original state plus ``context`` (retrieved memory)
        and whatever :meth:`gather` returned. Return ``None`` to skip the LLM
        call entirely (the agent then produces :meth:`empty_output`).
        """

    def parse(self, response: str) -> Any:
        """Parse the raw LLM response. Override for custom fallbacks."""
        out = parse_llm_json(response, self.empty_output())
        if self.parse_as == "list" and not isinstance(out, list):
            out = [out] if out else []
        return out

    async def act(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
        output: Any,
    ) -> dict[str, Any]:
        """Optional side effects (safety validation + deployment).

        ``working`` exposes everything :meth:`gather` collected (tool data,
        retrieved ``context``). Returns metadata recorded in the audit log and
        run status (e.g. ``{"deployed": True, "safety_result": {...}}``).
        """
        return {}

    def finalize(self, output: Any, action: dict[str, Any]) -> Any:
        """Map the parsed output to the value stored in ``state[produces]``.

        Defaults to the output unchanged. Override to, e.g., wrap a single
        design in a list so downstream count logic works uniformly.
        """
        return output

    def memory_entries(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
        output: Any,
        action: dict[str, Any],
    ) -> list[MemoryEntry]:
        """Return content to persist to long-term memory. Defaults to none."""
        return []

    # --- template method ----------------------------------------------------

    async def run(self, ctx: AgentContext, state: dict[str, Any]) -> AgentResult:
        """Execute the full agent lifecycle. Do not override — override hooks."""
        working: dict[str, Any] = dict(state)
        working["context"] = await self.retrieve_context(ctx)
        working.update(await self.gather(ctx, state, working))

        prompt = self.build_prompt(ctx, state, working)
        if prompt is None:
            output = self.empty_output()
        else:
            response = await chat_completion(
                model_tier=self.model_tier,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt},
                ],
            )
            output = self.parse(response)

        working[self.produces] = output
        action = await self.act(ctx, state, working, output)
        await self.persist(ctx, state, working, output, action)
        return AgentResult(output=self.finalize(output, action), metadata=action)

    # --- framework-provided helpers -----------------------------------------

    async def retrieve_context(self, ctx: AgentContext) -> str:
        """Semantic-search long-term memory for context relevant to this agent."""
        if not self.memory_query:
            return ""
        try:
            memories = await ctx.vector_store.search(
                project_id=ctx.project_id,
                query=self.memory_query,
                top_k=self.memory_top_k,
            )
            return "\n---\n".join(m["content"] for m in memories)
        except Exception as exc:  # memory is best-effort; never fail the run
            logger.warning("[%s] context retrieval failed: %s", self.name, exc)
            return ""

    async def persist(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
        output: Any,
        action: dict[str, Any],
    ) -> None:
        """Store any memory entries the agent produced."""
        for entry in self.memory_entries(ctx, state, working, output, action):
            try:
                await ctx.vector_store.store(
                    project_id=ctx.project_id,
                    content=entry.content,
                    metadata=entry.metadata,
                )
            except Exception as exc:
                logger.error("[%s] failed to store memory: %s", self.name, exc)

    def empty_output(self) -> Any:
        """The output value when reasoning is skipped."""
        return [] if self.parse_as == "list" else {}

    @classmethod
    def requirements_met(cls, state: dict[str, Any]) -> bool:
        """True if every key in ``requires`` is present and truthy in state."""
        return all(state.get(key) for key in cls.requires)
