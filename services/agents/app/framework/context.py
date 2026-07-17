"""Shared execution context and result types for the agent framework.

``AgentContext`` bundles the long-lived services an agent needs (database
pool, vector memory, audit logger) together with the run-scoped parameters
(run id, project, autonomy level). Passing one object replaces the old
pattern of smuggling ``_vector_store`` through the state dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import asyncpg

from app.memory.pgvector_store import PgVectorStore
from app.llm.contracts import (
    DataClassification,
    ExecutionKind,
    LlmRequestContext,
)
from app.safety.audit import AuditLogger


@dataclass
class AgentContext:
    """Services and run-scoped parameters available to every agent."""

    pool: asyncpg.Pool
    vector_store: PgVectorStore
    audit: AuditLogger
    run_id: str
    project_id: str
    execution_kind: ExecutionKind = "agent_run"
    #: Exact durable lease owner for a normal run. LLM governance binds egress
    #: to this value so a stale supervisor cannot borrow a replacement lease.
    lease_owner_id: str | None = None
    autonomy_level: int = 2
    time_range_days: int = 7
    #: When set, a forked code_implementation run targets exactly this proposal
    #: (one PR per approved proposal) instead of draining the project queue.
    target_proposal_id: str | None = None

    def llm_request(
        self,
        *,
        purpose: str,
        data_classification: DataClassification,
    ) -> LlmRequestContext:
        """Build the required immutable governance scope for one LLM call."""
        return LlmRequestContext(
            pool=self.pool,
            project_id=self.project_id,
            run_id=self.run_id,
            execution_kind=self.execution_kind,
            purpose=purpose,
            data_classification=data_classification,
            execution_owner_id=(
                self.lease_owner_id
                if self.execution_kind == "agent_run"
                else self.run_id
            ),
        )


@dataclass
class MemoryEntry:
    """A single piece of content an agent wants to persist to long-term memory."""

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    """The outcome of one agent run.

    ``output`` is the value stored into the shared supervisor state under the
    agent's ``produces`` key. ``metadata`` carries side information for audit
    logging and run-status updates (counts, deploy/approval flags, safety
    results). ``error`` is set when the run failed in a recoverable way.
    """

    output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    memory_entries: tuple[MemoryEntry, ...] = ()
    error: str | None = None
