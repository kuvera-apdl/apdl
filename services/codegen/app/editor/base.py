"""The editing-engine seam.

``Editor`` is the interface codegen uses to turn a task spec into a pushed
branch. Production uses an Aider-backed implementation (model-agnostic via
LiteLLM); tests use a fake. Keeping the engine behind a Protocol makes the engine
— and the model — a config choice, not a rewrite (plan decision D3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.contracts.models import ContractBundle
from app.inspection.models import DependencySlice, InspectionSnapshot
from app.requirements.models import RequirementLedger
from app.runtime.models import (
    GeneratedRuntimeWorkflowAttestation,
    RuntimeAcceptancePlan,
    RuntimeAcceptancePolicy,
)
from app.semantic_review.models import ReviewVerdict
from app.verification.models import VerificationCoverage, VerificationPlan


@dataclass
class EditRequest:
    """Everything the engine needs to implement one change and push a branch."""

    repo: str  # owner/name
    base_branch: str
    branch: str  # branch the engine must create or update and push
    token: str  # short-lived installation token, scoped to this repo
    title: str
    spec: str
    #: Tenant boundary for private dependency-contract caches. Legacy/custom
    #: callers may omit it; the editor then scopes evidence to ``repo``.
    project_scope: str = ""
    #: Stable requirement contract reused for same-PR CI repairs. Initial runs
    #: omit it and compile one deterministically from the task source.
    requirement_ledger: RequirementLedger | None = None
    inspection_snapshot: InspectionSnapshot | None = None
    dependency_slice: DependencySlice | None = None
    verification_plan: VerificationPlan | None = None
    verification_coverage: VerificationCoverage | None = None
    runtime_acceptance_plan: RuntimeAcceptancePlan | None = None
    runtime_acceptance_policy: RuntimeAcceptancePolicy = field(
        default_factory=RuntimeAcceptancePolicy
    )
    constraints: list[str] = field(default_factory=list)
    #: Repo verification command exposed as guidance so the generated change
    #: includes compatible tests. GitHub CI, not APDL, executes it authoritatively.
    test_cmd: str | None = None
    #: Connection-policy overrides for the pre-push gates (``policy["gates"]``).
    #: The engine evaluates the gates on the FULL diff before it pushes, so a
    #: violating branch never reaches the remote.
    gates_policy: dict[str, Any] | None = None
    #: Merge-commit SHA to revert deterministically (``git revert``) instead of
    #: asking the agent to reconstruct the revert from prose. The agent is still
    #: invoked afterwards if verification fails on the reverted tree.
    revert_sha: str | None = None
    #: Update the already-pushed PR branch instead of cutting a new branch.
    existing_branch: bool = False
    #: Exact failed PR head a repair is allowed to extend. A mismatch blocks
    #: editing and the push uses an explicit force-with-lease for this SHA.
    expected_head_sha: str | None = None
    #: Risk controls whether unavailable/unparseable auxiliary model gates may
    #: fail open. Only low-risk changes may skip them.
    risk_level: str = "low"


@dataclass
class EditResult:
    """Outcome of one edit attempt."""

    success: bool
    branch: str | None = None
    diff_stat: dict[str, Any] = field(default_factory=dict)
    changed_paths: list[str] = field(default_factory=list)
    diff_text: str = ""
    error: str | None = None
    logs_uri: str | None = None
    head_sha: str | None = None
    #: Exact installed dependency evidence used by this attempt. This is model
    #: grounding, never an APDL-local CI result.
    contract_bundle: ContractBundle | None = None
    requirement_ledger: RequirementLedger | None = None
    inspection_snapshot: InspectionSnapshot | None = None
    dependency_slice: DependencySlice | None = None
    verification_plan: VerificationPlan | None = None
    verification_coverage: VerificationCoverage | None = None
    runtime_acceptance_plan: RuntimeAcceptancePlan | None = None
    generated_runtime_workflow: GeneratedRuntimeWorkflowAttestation | None = None
    review_verdict: ReviewVerdict | None = None
    #: Ordered transcript of the LLM prompts this attempt actually sent — the
    #: brief compilation, each edit instruction handed to the coding agent, and
    #: each pre-push diff review. Entries are
    #: ``{"stage", "label", "system", "user", "notes"}`` dicts; ``system`` is
    #: ``None`` for the edit stage (Aider supplies its own system prompt).
    #: Populated on failure too — a failed run is exactly when an operator
    #: needs to see what the model was told.
    prompts: list[dict[str, Any]] = field(default_factory=list)


class Editor(Protocol):
    """Implements a change and pushes a branch.

    Editor implementations are trusted, push-capable service code: they receive
    a repository-scoped write token and must enforce the canonical gates before
    pushing. Model output is untrusted input to that implementation. A plugin
    that cannot satisfy this trust boundary must return a patch to a trusted
    editor adapter instead of implementing this protocol directly.

    Implementations MUST NOT raise for an ordinary failed attempt (editing or
    safety budget exhausted) — return ``EditResult(success=False, error=...)``
    so the job can record a clean ``tests_failed``. Reserve exceptions for
    genuinely unexpected faults.
    """

    async def implement(self, request: EditRequest) -> EditResult: ...
