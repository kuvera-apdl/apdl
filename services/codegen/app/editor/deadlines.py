"""One reconciled deadline plan for the credential-bearing editor pipeline.

The sandbox has a hard outer lifetime because it holds a short-lived GitHub
installation token.  Per-phase timeout knobs are therefore requested maxima,
not independent promises: when their complete worst-case schedule would exceed
the outer limit, this module scales the effective inner deadlines together.
That keeps every possible edit/review round inside the same explicit budget
instead of letting the outer container silently cut a legitimate phase short.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass


MAX_CODEGEN_JOB_BUDGET_SECONDS = 3000
CODEGEN_JOB_OVERHEAD_SECONDS = 60
CODEGEN_REMOTE_GIT_PHASES = 2  # authenticated clone + push
_MIN_PROCESS_TIMEOUT_SECONDS = 1
_MIN_LLM_TIMEOUT_SECONDS = 0.001


class CodegenDeadlineExceeded(TimeoutError):
    """The inner editor exhausted its share of the outer job lifetime."""


class CodegenRunDeadline:
    """One monotonic remaining-time boundary shared by every editor phase.

    The outer timeout starts before the sandbox process exists. Keep the fixed
    overhead reserve outside the inner deadline for container startup, result
    serialization, forced process cleanup, and token revocation. All inner
    subprocess and helper timeouts are clamped against this same clock, so time
    spent in inspection or contract resolution reduces later phase allowances
    instead of letting the outer container kill them without context.
    """

    def __init__(
        self,
        job_budget_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        active_seconds = job_budget_seconds - CODEGEN_JOB_OVERHEAD_SECONDS
        if active_seconds <= 0:
            raise ValueError(
                "Codegen job budget must exceed its fixed overhead reserve"
            )
        self._clock = clock
        self._expires_at = clock() + active_seconds

    def remaining_seconds(self) -> float:
        """Return non-negative wall time left for all inner work."""
        return max(0.0, self._expires_at - self._clock())

    def clamp_timeout(self, requested_seconds: int | float) -> float:
        """Cap one operation to the shared remaining wall-clock allowance."""
        requested = _positive_finite("operation timeout", requested_seconds)
        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise CodegenDeadlineExceeded(
                "Codegen job deadline was exhausted before the operation started"
            )
        return min(requested, remaining)


@dataclass(frozen=True)
class CodegenDeadlinePlan:
    """Effective inner deadlines and the outer credential-bearing lifetime."""

    job_budget_seconds: int
    agent_timeout_seconds: int
    git_timeout_seconds: int
    llm_timeout_seconds: float
    edit_rounds: int
    brief_calls: int
    review_calls: int
    requested_phase_seconds: float
    effective_phase_seconds: float

    @property
    def helper_calls(self) -> int:
        return self.brief_calls + self.review_calls

    @property
    def reserved_seconds(self) -> float:
        return CODEGEN_JOB_OVERHEAD_SECONDS + self.effective_phase_seconds

    @property
    def reconciled(self) -> bool:
        return not math.isclose(
            self.requested_phase_seconds,
            self.effective_phase_seconds,
            rel_tol=0,
            abs_tol=1e-9,
        )


def _positive_finite(name: str, value: int | float) -> float:
    resolved = float(value)
    if not math.isfinite(resolved) or resolved <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return resolved


def resolve_codegen_deadline_plan(
    *,
    agent_timeout_seconds: int,
    git_timeout_seconds: int,
    llm_timeout_seconds: float,
    edit_retries: int,
    brief_enabled: bool,
    review_enabled: bool,
    job_budget_override: int | None,
) -> CodegenDeadlinePlan:
    """Resolve requested phase maxima into one internally consistent plan.

    One edit-agent call is possible per round.  The brief runs at most once and
    semantic review runs at most once per round, including every retry.  The
    fixed overhead reserve covers deterministic inspection, serialization, and
    container startup/teardown outside those explicitly timed phases.
    """

    if edit_retries < 0:
        raise ValueError("CODEGEN_EDIT_RETRIES must not be negative")

    requested_agent = _positive_finite("CODEGEN_TIMEOUT", agent_timeout_seconds)
    requested_git = _positive_finite("CODEGEN_GIT_TIMEOUT", git_timeout_seconds)
    requested_llm = _positive_finite("CODEGEN_LLM_TIMEOUT", llm_timeout_seconds)
    rounds = 1 + edit_retries
    brief_calls = int(brief_enabled)
    review_calls = rounds if review_enabled else 0
    helper_calls = brief_calls + review_calls

    requested_phase_seconds = (
        rounds * requested_agent
        + CODEGEN_REMOTE_GIT_PHASES * requested_git
        + helper_calls * requested_llm
    )
    derived_budget = math.ceil(
        CODEGEN_JOB_OVERHEAD_SECONDS + requested_phase_seconds
    )

    budget_ceiling = MAX_CODEGEN_JOB_BUDGET_SECONDS
    if job_budget_override is not None:
        if job_budget_override < 1:
            raise ValueError("CODEGEN_JOB_BUDGET must be positive")
        if job_budget_override > MAX_CODEGEN_JOB_BUDGET_SECONDS:
            raise ValueError(
                "CODEGEN_JOB_BUDGET cannot exceed 3000 seconds while a GitHub "
                "write token is held"
            )
        budget_ceiling = job_budget_override
    job_budget = min(derived_budget, budget_ceiling)

    minimum_phase_seconds = (
        rounds * _MIN_PROCESS_TIMEOUT_SECONDS
        + CODEGEN_REMOTE_GIT_PHASES * _MIN_PROCESS_TIMEOUT_SECONDS
        + helper_calls * _MIN_LLM_TIMEOUT_SECONDS
    )
    minimum_job_budget = math.ceil(
        CODEGEN_JOB_OVERHEAD_SECONDS + minimum_phase_seconds
    )
    if job_budget < minimum_job_budget:
        raise ValueError(
            "CODEGEN_JOB_BUDGET is too small for the configured edit, git, "
            f"brief, and review phases; minimum is {minimum_job_budget} seconds"
        )

    available_phase_seconds = job_budget - CODEGEN_JOB_OVERHEAD_SECONDS
    scale = min(1.0, available_phase_seconds / requested_phase_seconds)
    effective_agent = max(
        _MIN_PROCESS_TIMEOUT_SECONDS, math.floor(requested_agent * scale)
    )
    effective_git = max(
        _MIN_PROCESS_TIMEOUT_SECONDS, math.floor(requested_git * scale)
    )
    effective_llm = requested_llm
    if helper_calls:
        effective_llm = max(_MIN_LLM_TIMEOUT_SECONDS, requested_llm * scale)

    effective_phase_seconds = (
        rounds * effective_agent
        + CODEGEN_REMOTE_GIT_PHASES * effective_git
        + helper_calls * effective_llm
    )
    if effective_phase_seconds > available_phase_seconds:
        # Integer process floors can only create a tiny excess at very small
        # budgets. Give the exact remainder to the fractional LLM deadline.
        non_llm_seconds = (
            rounds * effective_agent
            + CODEGEN_REMOTE_GIT_PHASES * effective_git
        )
        if not helper_calls:
            raise ValueError("CODEGEN_JOB_BUDGET cannot fit the configured phases")
        effective_llm = (
            available_phase_seconds - non_llm_seconds
        ) / helper_calls
        if effective_llm < _MIN_LLM_TIMEOUT_SECONDS:
            raise ValueError("CODEGEN_JOB_BUDGET cannot fit the configured phases")
        effective_phase_seconds = (
            non_llm_seconds + helper_calls * effective_llm
        )

    return CodegenDeadlinePlan(
        job_budget_seconds=job_budget,
        agent_timeout_seconds=effective_agent,
        git_timeout_seconds=effective_git,
        llm_timeout_seconds=effective_llm,
        edit_rounds=rounds,
        brief_calls=brief_calls,
        review_calls=review_calls,
        requested_phase_seconds=requested_phase_seconds,
        effective_phase_seconds=effective_phase_seconds,
    )
