"""Claude Managed Agents-backed Editor (plan decision D3 / Phase 2).

Drives the edit loop with Claude Managed Agents: an agent (``claude-opus-4-8`` at
``effort: xhigh`` with the ``agent_toolset_20260401`` file/bash tools) works in a
session whose repo is mounted as a ``github_repository`` resource, steered by a
test-green Outcome rubric. On success the agent commits and pushes the branch;
codegen opens the PR separately so merge gating stays in APDL.

INTEGRATION-UNTESTED: written against the Managed Agents API reference. It
requires the ``anthropic`` SDK and an Anthropic key/environment, and must be
validated against the live (beta) API before enabling. ``anthropic`` is imported
lazily so this module loads — and the tested execution path (``FakeEditor``)
runs — without it. Phase 3 swaps the cloud environment for a self-hosted worker
(plan D4); this is the cloud-first implementation.
"""

from __future__ import annotations

import logging
import os

from app.editor.base import EditRequest, EditResult

logger = logging.getLogger(__name__)

_MODEL = os.getenv("CODEGEN_MODEL", "claude-opus-4-8")
_EFFORT = os.getenv("CODEGEN_EFFORT", "xhigh")
_MAX_ITERATIONS = int(os.getenv("CODEGEN_MAX_ITERATIONS", "6"))

_SYSTEM_PROMPT = (
    "You are an autonomous software engineer working inside a cloned git "
    "repository. Implement the requested change, make the project's existing "
    "test suite pass, and leave the work committed on the requested branch. "
    "Match the codebase's existing conventions. Do not edit CI configuration, "
    "secrets, or files unrelated to the task."
)

_RUBRIC = (
    "The task is complete when ALL of the following hold:\n"
    "1. A git branch named `{branch}` exists, created from the base branch.\n"
    "2. The change described below is fully implemented.\n"
    "3. The project's test suite passes locally — actually run it; do not skip.\n"
    "4. All changes are committed to `{branch}` and pushed to origin.\n"
    "{constraints}\n\n"
    "Change to implement:\n{spec}\n"
)


class ManagedAgentsEditor:
    """Editor that delegates the edit loop to Claude Managed Agents."""

    def __init__(self, environment_id: str | None = None) -> None:
        self._environment_id = environment_id or os.getenv("CODEGEN_ENVIRONMENT_ID", "")

    async def implement(self, request: EditRequest) -> EditResult:
        try:
            return await self._run(request)
        except Exception as exc:  # an attempt must never raise to the job
            logger.exception("Managed Agents edit failed for %s", request.repo)
            return EditResult(success=False, branch=request.branch, error=str(exc))

    async def _run(self, request: EditRequest) -> EditResult:
        from anthropic import AsyncAnthropic  # lazy: keeps the dep optional for tests

        if not self._environment_id:
            raise RuntimeError("CODEGEN_ENVIRONMENT_ID is not configured.")

        client = AsyncAnthropic()
        agent = await client.beta.agents.create(
            name="apdl-codegen",
            model={"id": _MODEL, "effort": _EFFORT},
            system=_SYSTEM_PROMPT,
            tools=[{"type": "agent_toolset_20260401", "default_config": {"enabled": True}}],
        )
        session = await client.beta.sessions.create(
            agent=agent.id,
            environment_id=self._environment_id,
            resources=[
                {
                    "type": "github_repository",
                    "url": f"https://github.com/{request.repo}",
                    "authorization_token": request.token,
                    "checkout": {"type": "branch", "name": request.base_branch},
                }
            ],
        )

        rubric = _RUBRIC.format(
            branch=request.branch,
            spec=request.spec,
            constraints="\n".join(f"- {c}" for c in request.constraints),
        )
        stream = await client.beta.sessions.events.stream(session.id)
        await client.beta.sessions.events.send(
            session.id,
            events=[
                {
                    "type": "user.define_outcome",
                    "description": request.spec,
                    "rubric": {"type": "text", "content": rubric},
                    "max_iterations": _MAX_ITERATIONS,
                }
            ],
        )

        satisfied = False
        async for event in stream:
            if event.type == "span.outcome_evaluation_end":
                satisfied = event.result == "satisfied"
            elif event.type == "session.status_terminated":
                break
            elif event.type == "session.status_idle":
                # Break only on a terminal stop — `requires_action` is transient.
                if getattr(event.stop_reason, "type", "") != "requires_action":
                    break

        if not satisfied:
            return EditResult(
                success=False,
                branch=request.branch,
                error="Outcome not satisfied within the iteration budget.",
            )
        return EditResult(success=True, branch=request.branch, diff_stat={})
