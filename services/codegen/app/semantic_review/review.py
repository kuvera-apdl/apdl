"""Assemble model judgment and non-overridable deterministic semantic checks."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.contracts import ContractBundle
from app.inspection import DependencySlice
from app.requirements import ImplementationStatus, RequirementLedger
from app.semantic_review.checks import build_deterministic_findings
from app.semantic_review.models import (
    FindingSeverity,
    ModelResponseStatus,
    ModelReviewResponse,
    ProposedUncertainty,
    ReviewDecision,
    ReviewParseError,
    ReviewReferenceError,
    ReviewRequirementDecision,
    ReviewUncertainty,
    ReviewVerdict,
    UncertaintyCode,
)
from app.semantic_review.prompt import SEMANTIC_REVIEW_DIFF_CAP
from app.semantic_review.parser import parse_model_review_response
from app.semantic_review.references import (
    ReviewReferenceIndex,
    build_reference_index,
    validate_verdict_references,
)
from app.verification import (
    CoverageDisposition,
    VerificationCoverage,
    VerificationPlan,
)


@dataclass(frozen=True)
class _UncertaintyDraft:
    code: UncertaintyCode
    requirement_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    message: str
    instruction: str


def _active_ids(ledger: RequirementLedger) -> tuple[str, ...]:
    return tuple(
        requirement.requirement_id
        for requirement in ledger.requirements
        if requirement.implementation_status
        not in {ImplementationStatus.blocked, ImplementationStatus.descoped}
    )


def _slice_evidence_ids(dependency_slice: DependencySlice) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                item.evidence_id
                for group_name in (
                    "changed_files",
                    "imported_local_symbols",
                    "callers",
                    "routes_and_handlers",
                    "affected_tests",
                    "external_contracts",
                )
                for item in getattr(dependency_slice, group_name)
            }
        )
    )


def build_deterministic_uncertainties(
    *,
    ledger: RequirementLedger,
    contracts: ContractBundle,
    dependency_slice: DependencySlice,
    verification_plan: VerificationPlan,
    verification_coverage: VerificationCoverage,
    diff_text: str = "",
) -> list[ReviewUncertainty]:
    """Surface incomplete evidence explicitly; uncertainty is never approval."""
    active_ids = _active_ids(ledger)
    drafts: list[_UncertaintyDraft] = []
    slice_ids = _slice_evidence_ids(dependency_slice)
    if len(diff_text) > SEMANTIC_REVIEW_DIFF_CAP:
        drafts.append(
            _UncertaintyDraft(
                UncertaintyCode.diff_truncated,
                active_ids,
                slice_ids,
                "The proposed diff exceeded the semantic-review context budget.",
                "Split the change or review the omitted diff content independently.",
            )
        )
    if dependency_slice.truncated:
        drafts.append(
            _UncertaintyDraft(
                UncertaintyCode.dependency_slice_truncated,
                active_ids,
                slice_ids,
                "The dependency slice hit an inspection budget and may omit relevant context.",
                "Rebuild a complete focused slice before approving affected requirements.",
            )
        )
    if dependency_slice.unresolved_references:
        drafts.append(
            _UncertaintyDraft(
                UncertaintyCode.unresolved_reference,
                active_ids,
                slice_ids,
                "Repository references remain unresolved: "
                + "; ".join(dependency_slice.unresolved_references),
                "Resolve each reference or prove it is unrelated to the changed behavior.",
            )
        )

    active_set = set(active_ids)
    for resolution in contracts.resolutions:
        if resolution.disposition != "blocked":
            continue
        requirement_ids = tuple(
            value for value in resolution.request.requirement_ids if value in active_set
        ) or active_ids
        drafts.append(
            _UncertaintyDraft(
                UncertaintyCode.contract_blocked,
                requirement_ids,
                (),
                f"Exact dependency contract for {resolution.request.package_name!r} is blocked.",
                "Resolve the contract blocker against the exact installed version.",
            )
        )

    for requirement in ledger.requirements:
        if requirement.requirement_id not in active_set:
            continue
        text = (
            requirement.original_source_text + " " + requirement.observable_behavior
        )
        if re.search(r"\b(?:event|metric|tracking|exposure|analytics)\b", text, re.I) and not re.search(
            r"\b(?:event|metric|exposure)\s+[`'\"]([^`'\"]+)[`'\"]",
            text,
            re.I,
        ):
            evidence_ids = tuple(
                item.plan_item_id
                for item in verification_plan.items
                if item.requirement_id == requirement.requirement_id
            )
            drafts.append(
                _UncertaintyDraft(
                    UncertaintyCode.metric_contract_ambiguous,
                    (requirement.requirement_id,),
                    evidence_ids,
                    "The requirement asks for analytics behavior without an exact canonical event/metric identifier.",
                    "Name the canonical event or metric and its real sink before approval.",
                )
            )

    if verification_coverage.disposition is CoverageDisposition.unverified_external_ci:
        drafts.append(
            _UncertaintyDraft(
                UncertaintyCode.verification_unverified,
                active_ids,
                tuple(item.plan_item_id for item in verification_plan.items),
                verification_coverage.disposition_reason,
                "Add a GitHub-CI-compatible runner/workflow or keep the PR explicitly unverified.",
            )
        )
    elif (
        verification_coverage.disposition
        is CoverageDisposition.requires_protected_workflow_review
    ):
        drafts.append(
            _UncertaintyDraft(
                UncertaintyCode.protected_workflow_review,
                active_ids,
                tuple(item.plan_item_id for item in verification_plan.items),
                verification_coverage.disposition_reason,
                "Prove the protected workflow gates were preserved or strengthened.",
            )
        )

    drafts.sort(
        key=lambda item: (
            item.code.value,
            item.requirement_ids,
            item.message,
            item.evidence_ids,
        )
    )
    return [
        ReviewUncertainty(
            uncertainty_id=f"RU-{index:03d}",
            code=draft.code,
            requirement_ids=list(draft.requirement_ids),
            evidence_ids=list(draft.evidence_ids),
            message=draft.message,
            resolution_instruction=draft.instruction,
        )
        for index, draft in enumerate(drafts, start=1)
    ]


def _model_uncertainties(
    existing: list[ReviewUncertainty], proposed: list[ProposedUncertainty]
) -> list[ReviewUncertainty]:
    combined = [
        (
            item.code,
            tuple(item.requirement_ids),
            tuple(item.evidence_ids),
            item.message,
            item.resolution_instruction,
        )
        for item in existing
    ]
    combined.extend(
        (
            item.code,
            tuple(item.requirement_ids),
            tuple(item.evidence_ids),
            item.message,
            item.resolution_instruction,
        )
        for item in proposed
    )
    unique = sorted(
        set(combined),
        key=lambda item: (item[0].value, item[1], item[3], item[2]),
    )
    return [
        ReviewUncertainty(
            uncertainty_id=f"RU-{index:03d}",
            code=code,
            requirement_ids=list(requirement_ids),
            evidence_ids=list(evidence_ids),
            message=message,
            resolution_instruction=instruction,
        )
        for index, (code, requirement_ids, evidence_ids, message, instruction) in enumerate(
            unique, start=1
        )
    ]


def _fallback_response(
    *,
    active_ids: tuple[str, ...],
    status: ModelResponseStatus,
    reference_index: ReviewReferenceIndex,
) -> tuple[ModelReviewResponse, ProposedUncertainty]:
    code = (
        UncertaintyCode.model_response_unavailable
        if status is ModelResponseStatus.unavailable
        else UncertaintyCode.model_response_invalid
    )
    message = (
        "The independent reviewer model was unavailable."
        if status is ModelResponseStatus.unavailable
        else "The independent reviewer model returned an invalid strict response."
    )
    decisions = [
        ReviewRequirementDecision(
            requirement_id=requirement_id,
            decision=ReviewDecision.unverified,
            evidence_ids=[],
            rationale=message,
            actionable_instructions=["Run a valid independent evidence-backed review."],
        )
        for requirement_id in active_ids
    ]
    # Keep the argument explicit: fallback generation is tied to the exact
    # context even though it intentionally cites no evidence as approval.
    del reference_index
    uncertainty = ProposedUncertainty(
        code=code,
        requirement_ids=list(active_ids),
        evidence_ids=[],
        message=message,
        resolution_instruction="Run a valid independent evidence-backed review.",
    )
    return ModelReviewResponse(requirement_decisions=decisions), uncertainty


def _stable_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    return [value for value in values if value and not (value in seen or seen.add(value))]


def assemble_review_verdict(
    *,
    ledger: RequirementLedger,
    contracts: ContractBundle,
    dependency_slice: DependencySlice,
    verification_plan: VerificationPlan,
    verification_coverage: VerificationCoverage,
    diff_text: str,
    model_response_text: str | None,
) -> ReviewVerdict:
    """Build the final verdict; deterministic errors always win over approval."""
    reference_index = build_reference_index(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=verification_plan,
        verification_coverage=verification_coverage,
    )
    active_ids = _active_ids(ledger)
    findings = build_deterministic_findings(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=verification_plan,
        verification_coverage=verification_coverage,
        diff_text=diff_text,
    )
    uncertainties = build_deterministic_uncertainties(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=verification_plan,
        verification_coverage=verification_coverage,
        diff_text=diff_text,
    )

    fallback_uncertainty: ProposedUncertainty | None = None
    if model_response_text is None:
        model_status = ModelResponseStatus.unavailable
        model, fallback_uncertainty = _fallback_response(
            active_ids=active_ids,
            status=model_status,
            reference_index=reference_index,
        )
    else:
        try:
            model = parse_model_review_response(
                model_response_text, reference_index=reference_index
            )
            model_status = ModelResponseStatus.parsed
        except (ReviewParseError, ReviewReferenceError):
            model_status = ModelResponseStatus.invalid
            model, fallback_uncertainty = _fallback_response(
                active_ids=active_ids,
                status=model_status,
                reference_index=reference_index,
            )
    proposed_uncertainties = list(model.uncertainties)
    if fallback_uncertainty is not None:
        proposed_uncertainties.append(fallback_uncertainty)
    uncertainties = _model_uncertainties(uncertainties, proposed_uncertainties)

    model_by_id = {item.requirement_id: item for item in model.requirement_decisions}
    decisions: list[ReviewRequirementDecision] = []
    for requirement_id in active_ids:
        candidate = model_by_id[requirement_id]
        errors = [
            item
            for item in findings
            if item.severity is FindingSeverity.error
            and requirement_id in item.requirement_ids
        ]
        relevant_uncertainties = [
            item
            for item in uncertainties
            if not item.requirement_ids or requirement_id in item.requirement_ids
        ]
        if errors:
            decisions.append(
                ReviewRequirementDecision(
                    requirement_id=requirement_id,
                    decision=ReviewDecision.rejected,
                    evidence_ids=_stable_unique(
                        [
                            *candidate.evidence_ids,
                            *(evidence for item in errors for evidence in item.evidence_ids),
                        ]
                    ),
                    rationale="; ".join(item.message for item in errors),
                    actionable_instructions=_stable_unique(
                        [item.actionable_instruction for item in errors]
                    ),
                )
            )
        elif candidate.decision is ReviewDecision.rejected:
            decisions.append(candidate)
        elif relevant_uncertainties:
            decisions.append(
                ReviewRequirementDecision(
                    requirement_id=requirement_id,
                    decision=ReviewDecision.unverified,
                    evidence_ids=_stable_unique(
                        [
                            *candidate.evidence_ids,
                            *(
                                evidence
                                for item in relevant_uncertainties
                                for evidence in item.evidence_ids
                            ),
                        ]
                    ),
                    rationale="; ".join(item.message for item in relevant_uncertainties),
                    actionable_instructions=_stable_unique(
                        [item.resolution_instruction for item in relevant_uncertainties]
                    ),
                )
            )
        else:
            decisions.append(candidate)

    if any(item.decision is ReviewDecision.rejected for item in decisions) or any(
        item.severity is FindingSeverity.error for item in findings
    ):
        overall = ReviewDecision.rejected
    elif any(item.decision is ReviewDecision.unverified for item in decisions) or uncertainties:
        overall = ReviewDecision.unverified
    else:
        overall = ReviewDecision.approved

    actions = []
    if overall is not ReviewDecision.approved:
        actions = _stable_unique(
            [
                *(item.actionable_instruction for item in findings if item.severity is FindingSeverity.error),
                *(item.resolution_instruction for item in uncertainties),
                *(instruction for item in decisions for instruction in item.actionable_instructions),
                *model.actionable_instructions,
            ]
        )
    verdict = ReviewVerdict(
        reviewed_diff_sha256=hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
        overall_decision=overall,
        model_response_status=model_status,
        requirement_decisions=decisions,
        deterministic_findings=findings,
        uncertainties=uncertainties,
        actionable_instructions=actions,
    )
    validate_verdict_references(verdict, reference_index)
    return verdict
