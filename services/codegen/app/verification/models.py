"""Strict, versioned contracts for risk-based GitHub CI verification planning."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.requirements import RequirementRisk


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class VerificationSurface(str, Enum):
    general = "general"
    ui = "ui"
    api = "api"
    sdk = "sdk"
    analytics = "analytics"
    database = "database"
    security = "security"
    billing = "billing"
    concurrency = "concurrency"


class VerificationCheck(str, Enum):
    regression = "regression"
    render = "render"
    interaction = "interaction"
    accessibility_smoke = "accessibility_smoke"
    responsive_browser = "responsive_browser"
    route_existence = "route_existence"
    strict_request_response_schema = "strict_request_response_schema"
    error_cases = "error_cases"
    exact_version_contract = "exact_version_contract"
    lifecycle = "lifecycle"
    readiness = "readiness"
    cleanup = "cleanup"
    canonical_event = "canonical_event"
    real_sink = "real_sink"
    identity_consistency = "identity_consistency"
    exposure_and_metric = "exposure_and_metric"
    migration_execution = "migration_execution"
    rollback_or_forward_compatibility = "rollback_or_forward_compatibility"
    real_database_integration = "real_database_integration"
    unauthorized_path = "unauthorized_path"
    authorized_path = "authorized_path"
    secret_and_permission_checks = "secret_and_permission_checks"
    decimal_and_rounding = "decimal_and_rounding"
    idempotency = "idempotency"
    retry_behavior = "retry_behavior"
    race_behavior = "race_behavior"
    uniqueness = "uniqueness"
    transactionality = "transactionality"


class VerificationPolicyRule(StrictModel):
    check: VerificationCheck
    description: str = Field(min_length=1)


class VerificationPolicyPack(StrictModel):
    schema_version: Literal["verification_policy_pack@1"] = (
        "verification_policy_pack@1"
    )
    surface: VerificationSurface
    rules: list[VerificationPolicyRule] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_checks(self) -> VerificationPolicyPack:
        checks = [rule.check for rule in self.rules]
        if len(checks) != len(set(checks)):
            raise ValueError("policy-pack checks must be unique")
        return self


class PlanDisposition(str, Enum):
    github_ci_planned = "github_ci_planned"
    unverified_external_ci = "unverified_external_ci"
    no_implementable_requirements = "no_implementable_requirements"


class PlanItemDisposition(str, Enum):
    required_in_github_ci = "required_in_github_ci"
    unverified_external_ci = "unverified_external_ci"


class TestCommand(StrictModel):
    command: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    source_path: str = Field(min_length=1)


class VerificationPlanItem(StrictModel):
    plan_item_id: str = Field(pattern=r"^VP-[0-9]{3}$")
    requirement_id: str = Field(pattern=r"^REQ-[0-9]{3}$")
    surface: VerificationSurface
    policy_check: VerificationCheck
    requirement_risk: RequirementRisk
    expected_assertion: str = Field(min_length=1)
    expected_ci_evidence_ids: list[str] = Field(min_length=1)
    requires_changed_test_for_pr: bool
    disposition: PlanItemDisposition

    @model_validator(mode="after")
    def unique_evidence(self) -> VerificationPlanItem:
        if len(self.expected_ci_evidence_ids) != len(
            set(self.expected_ci_evidence_ids)
        ):
            raise ValueError("plan-item CI evidence IDs must be unique")
        return self


class VerificationPlan(StrictModel):
    schema_version: Literal["verification_plan@1"] = "verification_plan@1"
    source_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repo_profile_schema_version: str = Field(min_length=1)
    risk: RequirementRisk
    authority: Literal["github_ci"] = "github_ci"
    apdl_local_execution_authoritative: Literal[False] = False
    workflow_gate_policy: Literal["preserve_or_strengthen"] = (
        "preserve_or_strengthen"
    )
    test_runner_configured: bool
    test_commands: list[TestCommand] = Field(default_factory=list)
    github_workflow_paths: list[str] = Field(default_factory=list)
    protected_workflow_paths: list[str] = Field(default_factory=list)
    disposition: PlanDisposition
    disposition_reason: str = Field(min_length=1)
    items: list[VerificationPlanItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_plan(self) -> VerificationPlan:
        item_ids = [item.plan_item_id for item in self.items]
        expected = [f"VP-{index:03d}" for index in range(1, len(item_ids) + 1)]
        if item_ids != expected:
            raise ValueError("plan item IDs must be contiguous and ordered from VP-001")
        if len(self.github_workflow_paths) != len(set(self.github_workflow_paths)):
            raise ValueError("GitHub workflow paths must be unique")
        if self.github_workflow_paths != sorted(self.github_workflow_paths):
            raise ValueError("GitHub workflow paths must be sorted")
        if not set(self.protected_workflow_paths).issubset(
            self.github_workflow_paths
        ):
            raise ValueError("protected workflows must be known GitHub workflows")
        if self.protected_workflow_paths != sorted(
            set(self.protected_workflow_paths)
        ):
            raise ValueError("protected workflow paths must be sorted and unique")

        if self.disposition is PlanDisposition.github_ci_planned:
            if not self.test_runner_configured or not self.github_workflow_paths:
                raise ValueError(
                    "github_ci_planned requires a test runner and GitHub workflow"
                )
            if any(
                item.disposition is not PlanItemDisposition.required_in_github_ci
                for item in self.items
            ):
                raise ValueError("planned GitHub CI items must be required in GitHub CI")
        elif self.disposition is PlanDisposition.unverified_external_ci:
            if any(
                item.disposition is not PlanItemDisposition.unverified_external_ci
                for item in self.items
            ):
                raise ValueError("unverified plans must mark every item unverified")
        elif self.items:
            raise ValueError("no_implementable_requirements plans cannot contain items")
        return self


class CoverageDisposition(str, Enum):
    ready_for_github_ci = "ready_for_github_ci"
    missing_required_coverage = "missing_required_coverage"
    unverified_external_ci = "unverified_external_ci"
    requires_protected_workflow_review = "requires_protected_workflow_review"
    rejected_workflow_gate_relaxation = "rejected_workflow_gate_relaxation"
    no_implementable_requirements = "no_implementable_requirements"


class CoverageItemStatus(str, Enum):
    coverage_path_present = "coverage_path_present"
    missing_required_coverage = "missing_required_coverage"
    planned_in_github_ci = "planned_in_github_ci"
    unverified_external_ci = "unverified_external_ci"
    requires_protected_workflow_review = "requires_protected_workflow_review"
    rejected_workflow_gate_relaxation = "rejected_workflow_gate_relaxation"


class VerificationCoverageItem(StrictModel):
    plan_item_id: str = Field(pattern=r"^VP-[0-9]{3}$")
    status: CoverageItemStatus
    coverage_paths: list[str] = Field(default_factory=list)


class VerificationCoverage(StrictModel):
    schema_version: Literal["verification_coverage@1"] = "verification_coverage@1"
    source_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority: Literal["github_ci"] = "github_ci"
    github_has_not_reported: Literal[True] = True
    apdl_declared_verified: Literal[False] = False
    workflow_gate_policy: Literal["preserve_or_strengthen"] = (
        "preserve_or_strengthen"
    )
    disposition: CoverageDisposition
    disposition_reason: str = Field(min_length=1)
    changed_test_paths: list[str] = Field(default_factory=list)
    changed_workflow_paths: list[str] = Field(default_factory=list)
    policy_authorized_workflow_paths: list[str] = Field(default_factory=list)
    changed_protected_workflow_paths: list[str] = Field(default_factory=list)
    relaxed_workflow_paths: list[str] = Field(default_factory=list)
    items: list[VerificationCoverageItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def sorted_unique_paths(self) -> VerificationCoverage:
        for field_name in (
            "changed_test_paths",
            "changed_workflow_paths",
            "policy_authorized_workflow_paths",
            "changed_protected_workflow_paths",
            "relaxed_workflow_paths",
        ):
            paths = getattr(self, field_name)
            if paths != sorted(set(paths)):
                raise ValueError(f"{field_name} must be sorted and unique")
        if not set(self.changed_protected_workflow_paths).issubset(
            self.changed_workflow_paths
        ):
            raise ValueError("changed protected workflows must be changed workflows")
        if not set(self.policy_authorized_workflow_paths).issubset(
            self.changed_workflow_paths
        ):
            raise ValueError("policy-authorized workflows must be changed workflows")
        if set(self.policy_authorized_workflow_paths).intersection(
            self.relaxed_workflow_paths
        ):
            raise ValueError("a workflow cannot be both authorized and relaxed")
        return self
