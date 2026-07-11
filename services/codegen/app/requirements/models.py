"""Strict canonical contracts for requirement planning and verification.

The ledger is the durable boundary between a product-altitude task and the
editing/review pipeline.  It deliberately separates what was requested from
how an implementation is expected to prove it.  GitHub remains the authority
that eventually supplies the actual CI observations; this schema records only
the evidence that is expected before those observations exist.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_REQUIREMENT_ID = re.compile(r"^REQ-[0-9]{3}$")


class StrictModel(BaseModel):
    """Base for canonical contracts: no aliases, coercion, or extra fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class RequirementSourceKind(str, Enum):
    task_spec = "task_spec"
    acceptance_criterion = "acceptance_criterion"
    constraint = "constraint"


class RequirementRisk(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class ImplementationStatus(str, Enum):
    planned = "planned"
    implemented = "implemented"
    confirmed_existing = "confirmed_existing"
    blocked = "blocked"
    descoped = "descoped"


class ImplementationEvidenceKind(str, Enum):
    changed = "changed"
    existing = "existing"


class LikelyTarget(StrictModel):
    path: str = Field(min_length=1)
    symbol: str | None = None


class ImplementationEvidence(StrictModel):
    kind: ImplementationEvidenceKind
    path: str = Field(min_length=1)
    symbol: str | None = None
    description: str = Field(min_length=1)


class GitHubCheckExpectation(StrictModel):
    """An exact GitHub check-run or commit-status context expected to report."""

    kind: Literal["github_check"] = "github_check"
    evidence_id: str = Field(pattern=r"^CI-REQ-[0-9]{3}-[0-9]{2}$")
    check_name: str = Field(min_length=1)
    assertion: str = Field(min_length=1)


class RepositoryCommandExpectation(StrictModel):
    """A repository command GitHub CI is expected to execute."""

    kind: Literal["repository_command"] = "repository_command"
    evidence_id: str = Field(pattern=r"^CI-REQ-[0-9]{3}-[0-9]{2}$")
    command: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    assertion: str = Field(min_length=1)


class ObservableAssertionExpectation(StrictModel):
    """An observable assertion when no concrete CI job is known yet.

    This expectation never claims that CI exists or passed.  A repository with
    no matching GitHub signal remains externally unverified.
    """

    kind: Literal["observable_assertion"] = "observable_assertion"
    evidence_id: str = Field(pattern=r"^CI-REQ-[0-9]{3}-[0-9]{2}$")
    assertion: str = Field(min_length=1)


ExpectedCIEvidence = Annotated[
    GitHubCheckExpectation
    | RepositoryCommandExpectation
    | ObservableAssertionExpectation,
    Field(discriminator="kind"),
]


class Requirement(StrictModel):
    requirement_id: str = Field(pattern=r"^REQ-[0-9]{3}$")
    source_kind: RequirementSourceKind
    original_source_text: str = Field(min_length=1)
    observable_behavior: str = Field(min_length=1)
    implementable_scope: str = Field(min_length=1)
    likely_targets: list[LikelyTarget] = Field(default_factory=list)
    required_contract_evidence_ids: list[str] = Field(default_factory=list)
    expected_ci_evidence: list[ExpectedCIEvidence] = Field(default_factory=list)
    risk: RequirementRisk
    implementation_status: ImplementationStatus = ImplementationStatus.planned
    implementation_evidence: list[ImplementationEvidence] = Field(default_factory=list)
    decision_reason: str | None = None

    @model_validator(mode="after")
    def validate_status_evidence(self) -> Requirement:
        """Enforce the implementation and expected-CI mapping contract."""
        status = self.implementation_status
        terminal_decision = status in {
            ImplementationStatus.blocked,
            ImplementationStatus.descoped,
        }

        if terminal_decision:
            if not self.decision_reason or not self.decision_reason.strip():
                raise ValueError("blocked or descoped requirements need a decision_reason")
            if self.implementation_evidence:
                raise ValueError(
                    "blocked or descoped requirements cannot claim implementation evidence"
                )
            if self.expected_ci_evidence:
                raise ValueError(
                    "blocked or descoped requirements cannot claim expected CI evidence"
                )
        else:
            if self.decision_reason is not None:
                raise ValueError(
                    "decision_reason is only valid for blocked or descoped requirements"
                )
            if not self.expected_ci_evidence:
                raise ValueError(
                    "active requirements need at least one expected CI evidence mapping"
                )

        if status is ImplementationStatus.planned and self.implementation_evidence:
            raise ValueError("planned requirements cannot claim implementation evidence")
        if status is ImplementationStatus.implemented:
            if not any(
                item.kind is ImplementationEvidenceKind.changed
                for item in self.implementation_evidence
            ):
                raise ValueError(
                    "implemented requirements need evidence of a changed file or symbol"
                )
        if status is ImplementationStatus.confirmed_existing:
            if not self.implementation_evidence or any(
                item.kind is not ImplementationEvidenceKind.existing
                for item in self.implementation_evidence
            ):
                raise ValueError(
                    "confirmed_existing requirements need only existing-behavior evidence"
                )

        if len(self.required_contract_evidence_ids) != len(
            set(self.required_contract_evidence_ids)
        ):
            raise ValueError("contract evidence IDs must be unique per requirement")
        if any(not value.strip() for value in self.required_contract_evidence_ids):
            raise ValueError("contract evidence IDs cannot be blank")

        evidence_ids = [item.evidence_id for item in self.expected_ci_evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("expected CI evidence IDs must be unique per requirement")
        expected_prefix = f"CI-{self.requirement_id}-"
        if any(not value.startswith(expected_prefix) for value in evidence_ids):
            raise ValueError(
                "expected CI evidence IDs must be namespaced by requirement_id"
            )
        return self


class RequirementLedger(StrictModel):
    schema_version: Literal["requirement_ledger@1"] = "requirement_ledger@1"
    title: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requirements: list[Requirement] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ledger_identity(self) -> RequirementLedger:
        ids = [requirement.requirement_id for requirement in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("requirement IDs must be unique")
        expected_ids = [f"REQ-{index:03d}" for index in range(1, len(ids) + 1)]
        if ids != expected_ids:
            raise ValueError("requirement IDs must be contiguous and ordered from REQ-001")
        if any(not _REQUIREMENT_ID.fullmatch(value) for value in ids):
            raise ValueError("invalid requirement ID")

        evidence_ids = [
            evidence.evidence_id
            for requirement in self.requirements
            for evidence in requirement.expected_ci_evidence
        ]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("expected CI evidence IDs must be unique across the ledger")
        return self

    def ready_for_pull_request(self) -> bool:
        """Whether every implementable requirement has concrete code evidence."""
        return all(
            requirement.implementation_status
            in {
                ImplementationStatus.implemented,
                ImplementationStatus.confirmed_existing,
                ImplementationStatus.blocked,
                ImplementationStatus.descoped,
            }
            for requirement in self.requirements
        )
