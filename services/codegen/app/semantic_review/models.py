"""Strict canonical schemas for evidence-backed semantic review."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReviewDecision(str, Enum):
    approved = "approved"
    rejected = "rejected"
    unverified = "unverified"


class ModelResponseStatus(str, Enum):
    parsed = "parsed"
    unavailable = "unavailable"
    invalid = "invalid"


class FindingSeverity(str, Enum):
    error = "error"
    warning = "warning"


class FindingCode(str, Enum):
    missing_route_or_link = "missing_route_or_link"
    missing_cleanup = "missing_cleanup"
    dropped_handler_prop = "dropped_handler_prop"
    duplicate_initialization = "duplicate_initialization"
    strict_schema_violation = "strict_schema_violation"
    absent_metric = "absent_metric"
    async_readiness = "async_readiness"
    missing_contract_evidence = "missing_contract_evidence"
    missing_verification_coverage = "missing_verification_coverage"
    workflow_gate_relaxation = "workflow_gate_relaxation"


class UncertaintyCode(str, Enum):
    diff_truncated = "diff_truncated"
    dependency_slice_truncated = "dependency_slice_truncated"
    unresolved_reference = "unresolved_reference"
    contract_blocked = "contract_blocked"
    verification_unverified = "verification_unverified"
    protected_workflow_review = "protected_workflow_review"
    model_response_unavailable = "model_response_unavailable"
    model_response_invalid = "model_response_invalid"
    metric_contract_ambiguous = "metric_contract_ambiguous"


class ReviewRequirementDecision(StrictModel):
    requirement_id: str = Field(pattern=r"^REQ-[0-9]{3}$")
    decision: ReviewDecision
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    actionable_instructions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_decision_payload(self) -> ReviewRequirementDecision:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("decision evidence IDs must be unique")
        if any(not value.strip() for value in self.evidence_ids):
            raise ValueError("decision evidence IDs cannot be blank")
        if self.decision in {ReviewDecision.approved, ReviewDecision.rejected}:
            if not self.evidence_ids:
                raise ValueError("approved/rejected decisions require evidence")
        if self.decision is ReviewDecision.approved and self.actionable_instructions:
            raise ValueError("approved decisions cannot contain fix instructions")
        if self.decision in {ReviewDecision.rejected, ReviewDecision.unverified}:
            if not self.actionable_instructions:
                raise ValueError("rejected/unverified decisions need actionable instructions")
        if any(not item.strip() for item in self.actionable_instructions):
            raise ValueError("actionable instructions cannot be blank")
        return self


class ProposedUncertainty(StrictModel):
    code: UncertaintyCode
    requirement_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    message: str = Field(min_length=1)
    resolution_instruction: str = Field(min_length=1)


class ModelReviewResponse(StrictModel):
    """The exact JSON shape requested from the independent reviewer model."""

    schema_version: Literal["review_model_response@1"] = "review_model_response@1"
    requirement_decisions: list[ReviewRequirementDecision] = Field(default_factory=list)
    uncertainties: list[ProposedUncertainty] = Field(default_factory=list)
    actionable_instructions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_requirements(self) -> ModelReviewResponse:
        ids = [item.requirement_id for item in self.requirement_decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("model requirement decisions must be unique")
        return self


class DeterministicFinding(StrictModel):
    finding_id: str = Field(pattern=r"^RF-[0-9]{3}$")
    code: FindingCode
    severity: FindingSeverity
    requirement_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    message: str = Field(min_length=1)
    actionable_instruction: str = Field(min_length=1)

    @model_validator(mode="after")
    def unique_references(self) -> DeterministicFinding:
        if len(self.requirement_ids) != len(set(self.requirement_ids)):
            raise ValueError("finding requirement IDs must be unique")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("finding evidence IDs must be unique")
        return self


class ReviewUncertainty(StrictModel):
    uncertainty_id: str = Field(pattern=r"^RU-[0-9]{3}$")
    code: UncertaintyCode
    requirement_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    message: str = Field(min_length=1)
    resolution_instruction: str = Field(min_length=1)

    @model_validator(mode="after")
    def unique_references(self) -> ReviewUncertainty:
        if len(self.requirement_ids) != len(set(self.requirement_ids)):
            raise ValueError("uncertainty requirement IDs must be unique")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("uncertainty evidence IDs must be unique")
        return self


class ReviewVerdict(StrictModel):
    schema_version: Literal["review_verdict@1"] = "review_verdict@1"
    reviewed_diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    overall_decision: ReviewDecision
    model_response_status: ModelResponseStatus
    deterministic_errors_override_model: Literal[True] = True
    requirement_decisions: list[ReviewRequirementDecision] = Field(default_factory=list)
    deterministic_findings: list[DeterministicFinding] = Field(default_factory=list)
    uncertainties: list[ReviewUncertainty] = Field(default_factory=list)
    actionable_instructions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_verdict_consistency(self) -> ReviewVerdict:
        decision_ids = [item.requirement_id for item in self.requirement_decisions]
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("verdict requirement decisions must be unique")
        finding_ids = [item.finding_id for item in self.deterministic_findings]
        if finding_ids != [
            f"RF-{index:03d}" for index in range(1, len(finding_ids) + 1)
        ]:
            raise ValueError("finding IDs must be contiguous and ordered from RF-001")
        uncertainty_ids = [item.uncertainty_id for item in self.uncertainties]
        if uncertainty_ids != [
            f"RU-{index:03d}" for index in range(1, len(uncertainty_ids) + 1)
        ]:
            raise ValueError("uncertainty IDs must be contiguous and ordered from RU-001")

        has_error = any(
            item.severity is FindingSeverity.error
            for item in self.deterministic_findings
        )
        has_rejection = any(
            item.decision is ReviewDecision.rejected
            for item in self.requirement_decisions
        )
        has_unverified = any(
            item.decision is ReviewDecision.unverified
            for item in self.requirement_decisions
        )
        if has_error or has_rejection:
            if self.overall_decision is not ReviewDecision.rejected:
                raise ValueError("deterministic errors/rejections require rejected overall")
        elif has_unverified or self.uncertainties:
            if self.overall_decision is not ReviewDecision.unverified:
                raise ValueError("unverified decisions/uncertainties require unverified overall")
        elif self.overall_decision is not ReviewDecision.approved:
            raise ValueError("fully evidenced decisions must be approved overall")

        if self.overall_decision is ReviewDecision.approved:
            if self.actionable_instructions:
                raise ValueError("approved verdicts cannot contain actionable instructions")
        elif not self.actionable_instructions:
            raise ValueError("rejected/unverified verdicts need actionable instructions")
        return self


class ReviewReferenceError(ValueError):
    """Raised when a review cites evidence outside the supplied context."""


class ReviewParseError(ValueError):
    """Raised when model output is not the exact strict response schema."""
