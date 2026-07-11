"""Cross-contract reference validation for semantic review records."""

from __future__ import annotations

from dataclasses import dataclass

from app.contracts import ContractBundle
from app.inspection import DependencySlice
from app.requirements import ImplementationStatus, RequirementLedger
from app.semantic_review.models import (
    ModelReviewResponse,
    ReviewReferenceError,
    ReviewVerdict,
)
from app.verification import VerificationCoverage, VerificationPlan


@dataclass(frozen=True)
class ReviewReferenceIndex:
    active_requirement_ids: frozenset[str]
    evidence_ids: frozenset[str]
    approval_evidence_ids: frozenset[str]


def build_reference_index(
    *,
    ledger: RequirementLedger,
    contracts: ContractBundle,
    dependency_slice: DependencySlice,
    verification_plan: VerificationPlan,
    verification_coverage: VerificationCoverage,
) -> ReviewReferenceIndex:
    active = frozenset(
        requirement.requirement_id
        for requirement in ledger.requirements
        if requirement.implementation_status
        not in {ImplementationStatus.blocked, ImplementationStatus.descoped}
    )
    evidence: set[str] = {
        item.evidence_id
        for requirement in ledger.requirements
        for item in requirement.expected_ci_evidence
    }
    approval_evidence: set[str] = set()
    for resolution in contracts.resolutions:
        if resolution.evidence is None:
            continue
        evidence.add(resolution.evidence.contract_id)
        approval_evidence.add(resolution.evidence.contract_id)
        evidence.update(source.source_id for source in resolution.evidence.sources)
        approval_evidence.update(
            source.source_id for source in resolution.evidence.sources
        )
    for group_name in (
        "changed_files",
        "imported_local_symbols",
        "callers",
        "routes_and_handlers",
        "affected_tests",
        "relevant_lockfiles",
        "external_contracts",
    ):
        group_ids = {
            item.evidence_id for item in getattr(dependency_slice, group_name)
        }
        evidence.update(group_ids)
        approval_evidence.update(group_ids)
    evidence.update(item.plan_item_id for item in verification_plan.items)
    evidence.update(
        evidence_id
        for item in verification_plan.items
        for evidence_id in item.expected_ci_evidence_ids
    )
    evidence.update(item.plan_item_id for item in verification_coverage.items)
    return ReviewReferenceIndex(
        active,
        frozenset(evidence),
        frozenset(approval_evidence),
    )


def _check_refs(
    *,
    requirement_ids: list[str],
    evidence_ids: list[str],
    index: ReviewReferenceIndex,
    label: str,
) -> None:
    unknown_requirements = sorted(set(requirement_ids) - index.active_requirement_ids)
    unknown_evidence = sorted(set(evidence_ids) - index.evidence_ids)
    if unknown_requirements or unknown_evidence:
        parts = []
        if unknown_requirements:
            parts.append("unknown requirements: " + ", ".join(unknown_requirements))
        if unknown_evidence:
            parts.append("unknown evidence: " + ", ".join(unknown_evidence))
        raise ReviewReferenceError(f"{label} references " + "; ".join(parts))


def validate_model_response_references(
    response: ModelReviewResponse, index: ReviewReferenceIndex
) -> None:
    decision_ids = {item.requirement_id for item in response.requirement_decisions}
    if decision_ids != index.active_requirement_ids:
        missing = sorted(index.active_requirement_ids - decision_ids)
        extra = sorted(decision_ids - index.active_requirement_ids)
        details = []
        if missing:
            details.append("missing decisions: " + ", ".join(missing))
        if extra:
            details.append("unknown decisions: " + ", ".join(extra))
        raise ReviewReferenceError("model response must decide every active requirement; " + "; ".join(details))
    for item in response.requirement_decisions:
        _check_refs(
            requirement_ids=[item.requirement_id],
            evidence_ids=item.evidence_ids,
            index=index,
            label=f"decision {item.requirement_id}",
        )
        if item.decision.value == "approved" and not set(item.evidence_ids).intersection(
            index.approval_evidence_ids
        ):
            raise ReviewReferenceError(
                f"decision {item.requirement_id} approval must cite repository "
                "or exact-contract evidence"
            )
    for uncertainty in response.uncertainties:
        _check_refs(
            requirement_ids=uncertainty.requirement_ids,
            evidence_ids=uncertainty.evidence_ids,
            index=index,
            label=f"uncertainty {uncertainty.code.value}",
        )


def validate_verdict_references(
    verdict: ReviewVerdict, index: ReviewReferenceIndex
) -> None:
    decision_ids = {item.requirement_id for item in verdict.requirement_decisions}
    if decision_ids != index.active_requirement_ids:
        raise ReviewReferenceError(
            "final verdict must contain exactly one decision per active requirement"
        )
    for item in verdict.requirement_decisions:
        _check_refs(
            requirement_ids=[item.requirement_id],
            evidence_ids=item.evidence_ids,
            index=index,
            label=f"decision {item.requirement_id}",
        )
    for finding in verdict.deterministic_findings:
        _check_refs(
            requirement_ids=finding.requirement_ids,
            evidence_ids=finding.evidence_ids,
            index=index,
            label=f"finding {finding.finding_id}",
        )
    for uncertainty in verdict.uncertainties:
        _check_refs(
            requirement_ids=uncertainty.requirement_ids,
            evidence_ids=uncertainty.evidence_ids,
            index=index,
            label=f"uncertainty {uncertainty.uncertainty_id}",
        )
