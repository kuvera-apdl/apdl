"""Bounded deterministic context rendering for independent semantic review."""

from __future__ import annotations

import json

from app.contracts import ContractBundle, render_contract_bundle
from app.inspection import DependencySlice, render_dependency_slice
from app.requirements import RequirementLedger, render_requirement_ledger
from app.semantic_review.models import DeterministicFinding, ReviewUncertainty
from app.verification import (
    VerificationCoverage,
    VerificationPlan,
    render_verification_coverage,
    render_verification_plan,
)

SEMANTIC_REVIEW_DIFF_CAP = 80_000

SEMANTIC_REVIEW_SYSTEM = """\
You review an automated code change independently from the editing agent. Use
only the supplied repository, dependency-contract, requirement, and verification
evidence. Return exactly one review_model_response@1 JSON object and no prose.
Never treat planned tests, changed test files, or APDL-local checks as passed
GitHub CI. Deterministic errors in the supplied context cannot be overridden.
"""


def _strict_output_shape() -> str:
    return json.dumps(
        {
            "schema_version": "review_model_response@1",
            "requirement_decisions": [
                {
                    "requirement_id": "REQ-001",
                    "decision": "unverified",
                    "evidence_ids": ["an ID from the supplied context"],
                    "rationale": "evidence-backed reason",
                    "actionable_instructions": [
                        "required for rejected/unverified; empty for approved"
                    ],
                }
            ],
            "uncertainties": [
                {
                    "code": "verification_unverified",
                    "requirement_ids": ["REQ-001"],
                    "evidence_ids": [],
                    "message": "what cannot be established",
                    "resolution_instruction": "how to resolve it",
                }
            ],
            "actionable_instructions": [],
        },
        indent=2,
    )


def render_semantic_review_prompt(
    *,
    ledger: RequirementLedger,
    contracts: ContractBundle,
    dependency_slice: DependencySlice,
    verification_plan: VerificationPlan,
    verification_coverage: VerificationCoverage,
    deterministic_findings: list[DeterministicFinding],
    deterministic_uncertainties: list[ReviewUncertainty],
    diff_text: str,
) -> str:
    """Render every canonical evidence boundary consumed by the reviewer."""
    diff = diff_text[:SEMANTIC_REVIEW_DIFF_CAP]
    if len(diff_text) > SEMANTIC_REVIEW_DIFF_CAP:
        diff += "\n[…diff truncated for semantic review…]"
    deterministic = json.dumps(
        {
            "findings": [item.model_dump(mode="json") for item in deterministic_findings],
            "uncertainties": [
                item.model_dump(mode="json") for item in deterministic_uncertainties
            ],
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return "\n\n".join(
        (
            "# Independent semantic review\n\n"
            "Decide every active requirement separately using only cited evidence IDs. "
            "A deterministic error is non-overridable: reject its affected requirements "
            "even if the diff otherwise looks plausible. An uncertainty is not approval. "
            "Do not treat a verification plan, changed test file, or APDL-local command "
            "as a passed GitHub result.",
            render_requirement_ledger(ledger),
            f"Contract bundle schema: `{contracts.schema_version}`\n\n"
            + render_contract_bundle(contracts),
            render_dependency_slice(dependency_slice),
            render_verification_plan(verification_plan),
            render_verification_coverage(verification_coverage),
            "## Deterministic review results\n\n```json\n"
            + deterministic
            + "\n```",
            "## Proposed diff\n\n```diff\n" + diff + "\n```",
            "## Required response\n\nReturn only this strict JSON shape, with one "
            "decision for every active requirement and no extra fields:\n\n```json\n"
            + _strict_output_shape()
            + "\n```",
        )
    )
