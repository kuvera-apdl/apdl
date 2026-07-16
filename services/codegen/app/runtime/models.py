"""Strict contracts for GitHub-owned runtime acceptance evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from app.models.observations import ExternalCIStatus

_REQUIREMENT_ID_PATTERN = r"^REQ-[0-9]{3}$"
_HEAD_SHA_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"
_ARTIFACT_NAME_PATTERN = r"^[A-Za-z0-9_.-]{1,128}$"
_POSTGRES_INTEGER_MAX = 2_147_483_647
RUNTIME_ACCEPTANCE_WORKFLOW_PATH = ".github/workflows/apdl-runtime-acceptance.yml"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RuntimeSurface(str, Enum):
    browser = "browser"
    api = "api"
    service_container = "service_container"
    runtime = "runtime"


class RuntimeEvidenceKind(str, Enum):
    screenshot = "screenshot"
    request_trace = "request_trace"
    emitted_events = "emitted_events"
    measurements = "measurements"
    browser_report = "browser_report"
    server_log = "server_log"
    structured_runtime = "structured_runtime"


class RuntimeEvidenceStatus(str, Enum):
    observed = "observed"
    unverified = "unverified"


class RuntimeAcceptanceRequest(StrictModel):
    """Tenant preference for runtime acceptance; never an execution grant."""

    schema_version: Literal["runtime_acceptance_request@1"] = (
        "runtime_acceptance_request@1"
    )
    enabled: StrictBool = False


class RuntimeAcceptancePolicy(StrictModel):
    """Trusted effective grant for the canonical generated Actions workflow.

    Service code constructs this only after intersecting the tenant request with
    operator-owned policy. The workflow path is deliberately absent from this
    contract so neither caller can redirect the grant to another workflow.
    """

    schema_version: Literal["runtime_acceptance_policy@2"] = (
        "runtime_acceptance_policy@2"
    )
    enabled: StrictBool = False


def _relative_path(value: str) -> str:
    normalized = value.replace("\\", "/").removeprefix("./")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or ".." in parts
        or any(character in normalized for character in ("\x00", "\r", "\n"))
    ):
        raise ValueError("runtime evidence paths must be repository-relative")
    return normalized


def _sorted_unique(values: list[str], field_name: str) -> list[str]:
    if values != sorted(set(values)):
        raise ValueError(f"{field_name} must be sorted and unique")
    return values


def _aware_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class RuntimeRequirement(StrictModel):
    requirement_id: str = Field(pattern=_REQUIREMENT_ID_PATTERN)
    surface: RuntimeSurface


class RuntimeCommand(StrictModel):
    command: str = Field(min_length=1, max_length=1000)
    cwd: str
    source_path: str

    @field_validator("command")
    @classmethod
    def single_line_command(cls, value: str) -> str:
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("runtime commands must be single-line repository facts")
        return value

    @field_validator("cwd", "source_path")
    @classmethod
    def validate_paths(cls, value: str) -> str:
        return "." if value == "." else _relative_path(value)


class RuntimeArtifactExpectation(StrictModel):
    schema_version: Literal["runtime_artifact_expectation@1"] = (
        "runtime_artifact_expectation@1"
    )
    artifact_name: str = Field(pattern=_ARTIFACT_NAME_PATTERN)
    evidence_kind: RuntimeEvidenceKind
    paths: list[str] = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    required: bool = True

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        normalized = [_relative_path(value) for value in values]
        return _sorted_unique(normalized, "runtime artifact paths")

    @field_validator("requirement_ids")
    @classmethod
    def validate_requirement_ids(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(_REQUIREMENT_ID_PATTERN, value) for value in values):
            raise ValueError("runtime requirement IDs must use REQ-NNN")
        return _sorted_unique(values, "runtime artifact requirement IDs")


class RuntimeCheck(StrictModel):
    check_id: str = Field(pattern=r"^runtime_[0-9a-f]{16}$")
    surface: RuntimeSurface
    requirement_ids: list[str] = Field(min_length=1)
    command: RuntimeCommand
    service_container_paths: list[str] = Field(default_factory=list)
    expected_artifacts: list[RuntimeArtifactExpectation] = Field(min_length=1)

    @field_validator("requirement_ids")
    @classmethod
    def validate_requirement_ids(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(_REQUIREMENT_ID_PATTERN, value) for value in values):
            raise ValueError("runtime requirement IDs must use REQ-NNN")
        return _sorted_unique(values, "runtime check requirement IDs")

    @field_validator("service_container_paths")
    @classmethod
    def validate_container_paths(cls, values: list[str]) -> list[str]:
        normalized = [_relative_path(value) for value in values]
        return _sorted_unique(normalized, "service-container paths")

    @model_validator(mode="after")
    def validate_artifact_coverage(self) -> RuntimeCheck:
        artifact_names = [item.artifact_name for item in self.expected_artifacts]
        if artifact_names != sorted(set(artifact_names)):
            raise ValueError("runtime artifact expectations must be sorted and unique")
        known_requirements = set(self.requirement_ids)
        artifact_requirements = {
            requirement_id
            for expectation in self.expected_artifacts
            for requirement_id in expectation.requirement_ids
        }
        if not artifact_requirements <= known_requirements:
            raise ValueError(
                "runtime artifacts cannot reference requirements outside their check"
            )
        required_coverage = {
            requirement_id
            for expectation in self.expected_artifacts
            if expectation.required
            for requirement_id in expectation.requirement_ids
        }
        if required_coverage != known_requirements:
            raise ValueError(
                "every runtime-check requirement needs required artifact coverage"
            )
        if self.surface is RuntimeSurface.service_container:
            if not self.service_container_paths:
                raise ValueError(
                    "service-container checks require declared deployment paths"
                )
        elif self.service_container_paths:
            raise ValueError("only service-container checks may carry deployment paths")
        return self


class RuntimeBlocker(StrictModel):
    requirement_id: str = Field(pattern=_REQUIREMENT_ID_PATTERN)
    surface: RuntimeSurface
    reason: str = Field(min_length=1)
    evidence_paths: list[str] = Field(default_factory=list)

    @field_validator("evidence_paths")
    @classmethod
    def validate_evidence_paths(cls, values: list[str]) -> list[str]:
        normalized = [_relative_path(value) for value in values]
        return _sorted_unique(normalized, "runtime blocker evidence paths")


class GeneratedRuntimeWorkflowExpectation(StrictModel):
    """Plan-bound identity of the only generated workflow APDL may exempt."""

    schema_version: Literal["generated_runtime_workflow_expectation@1"] = (
        "generated_runtime_workflow_expectation@1"
    )
    renderer: Literal["apdl_github_actions_runtime@1"] = (
        "apdl_github_actions_runtime@1"
    )
    path: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = _relative_path(value)
        if not normalized.startswith(".github/workflows/") or not normalized.endswith(
            (".yml", ".yaml")
        ):
            raise ValueError("generated runtime workflow must be a GitHub workflow")
        return normalized


class RuntimeAcceptancePlan(StrictModel):
    schema_version: Literal["runtime_acceptance_plan@1"] = "runtime_acceptance_plan@1"
    source_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repo_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repo: str | None = None
    branch: str | None = None
    checks: list[RuntimeCheck] = Field(default_factory=list)
    blockers: list[RuntimeBlocker] = Field(default_factory=list)
    generated_workflow: GeneratedRuntimeWorkflowExpectation | None = None

    def evidence_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), separators=(",", ":"), sort_keys=True
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    @model_validator(mode="after")
    def unique_checks_and_requirements(self) -> RuntimeAcceptancePlan:
        if self.generated_workflow is not None and not self.checks:
            raise ValueError("a generated runtime workflow requires executable checks")
        check_ids = [check.check_id for check in self.checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("runtime check IDs must be unique")
        artifact_names = [
            expectation.artifact_name
            for check in self.checks
            for expectation in check.expected_artifacts
        ]
        if len(artifact_names) != len(set(artifact_names)):
            raise ValueError("runtime artifact names must be unique across checks")
        if self.checks != sorted(
            self.checks, key=lambda check: (check.surface.value, check.check_id)
        ):
            raise ValueError("runtime checks must be sorted by surface and check ID")
        if self.blockers != sorted(
            self.blockers,
            key=lambda blocker: (blocker.requirement_id, blocker.surface.value),
        ):
            raise ValueError(
                "runtime blockers must be sorted by requirement and surface"
            )

        checked = [
            (requirement_id, check.surface)
            for check in self.checks
            for requirement_id in check.requirement_ids
        ]
        blocked = [
            (blocker.requirement_id, blocker.surface) for blocker in self.blockers
        ]
        if len(checked) != len(set(checked)):
            raise ValueError("a runtime requirement surface may have only one check")
        if len(blocked) != len(set(blocked)):
            raise ValueError("a runtime requirement surface may have only one blocker")
        if set(checked).intersection(blocked):
            raise ValueError(
                "a runtime requirement surface cannot be both checked and blocked"
            )
        return self


class GeneratedRuntimeWorkflowAttestation(StrictModel):
    """Editor attestation for the exact deterministic workflow bytes it pushed."""

    schema_version: Literal["generated_runtime_workflow_attestation@1"] = (
        "generated_runtime_workflow_attestation@1"
    )
    renderer: Literal["apdl_github_actions_runtime@1"] = (
        "apdl_github_actions_runtime@1"
    )
    path: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_acceptance_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = _relative_path(value)
        if not normalized.startswith(".github/workflows/") or not normalized.endswith(
            (".yml", ".yaml")
        ):
            raise ValueError("generated runtime workflow must be a GitHub workflow")
        return normalized


class RuntimeManifestEvidenceFile(StrictModel):
    path: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _relative_path(value)


class RuntimeManifestRequirement(StrictModel):
    requirement_id: str = Field(pattern=_REQUIREMENT_ID_PATTERN)
    status: Literal["passed"] = "passed"
    assertion: str = Field(min_length=1, max_length=2000)
    evidence_files: list[RuntimeManifestEvidenceFile] = Field(default_factory=list)

    @field_validator("evidence_files")
    @classmethod
    def validate_evidence_files(
        cls, values: list[RuntimeManifestEvidenceFile]
    ) -> list[RuntimeManifestEvidenceFile]:
        paths = [item.path for item in values]
        if paths != sorted(set(paths)):
            raise ValueError("runtime manifest evidence files must be sorted and unique")
        return values


class RuntimeEvidenceManifest(StrictModel):
    """Structured assertion results emitted by a GitHub runtime test harness."""

    schema_version: Literal["runtime_evidence_manifest@1"] = (
        "runtime_evidence_manifest@1"
    )
    head_sha: str = Field(pattern=_HEAD_SHA_PATTERN)
    requirements: list[RuntimeManifestRequirement] = Field(min_length=1)

    @field_validator("requirements")
    @classmethod
    def validate_requirements(
        cls, values: list[RuntimeManifestRequirement]
    ) -> list[RuntimeManifestRequirement]:
        ids = [item.requirement_id for item in values]
        if ids != sorted(set(ids)):
            raise ValueError("runtime manifest requirements must be sorted and unique")
        return values


class ArtifactFileEvidence(StrictModel):
    schema_version: Literal["runtime_artifact_file@1"] = "runtime_artifact_file@1"
    path: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=0)
    text_excerpt: str | None = Field(default=None, max_length=8000)
    redacted: bool = False
    binary: bool = False

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _relative_path(value)

    @model_validator(mode="after")
    def validate_content_shape(self) -> ArtifactFileEvidence:
        if self.binary and self.text_excerpt is not None:
            raise ValueError("binary runtime evidence cannot carry a text excerpt")
        if self.binary and self.redacted:
            raise ValueError("binary runtime evidence cannot be marked text-redacted")
        if self.redacted and self.text_excerpt is None:
            raise ValueError("redacted runtime evidence needs a text excerpt")
        return self


class RuntimeJobLogEvidence(StrictModel):
    """Bounded, redacted GitHub job-log evidence for one exact PR head."""

    schema_version: Literal["runtime_job_log_evidence@1"] = "runtime_job_log_evidence@1"
    workflow_run_id: int = Field(ge=1)
    job_id: int = Field(ge=1)
    job_name: str = Field(min_length=1, max_length=300)
    head_sha: str = Field(pattern=_HEAD_SHA_PATTERN)
    text_excerpt: str = Field(max_length=8000)
    excerpt_byte_count: int = Field(ge=0, le=8000)
    source_byte_count: int = Field(ge=0)
    truncated: bool
    redacted: bool
    github_url: str = Field(min_length=1, max_length=2000)

    @field_validator("github_url")
    @classmethod
    def validate_github_url(cls, value: str) -> str:
        if not value.startswith("https://") or any(
            character in value for character in ("\x00", "\r", "\n")
        ):
            raise ValueError("runtime job evidence requires an HTTPS GitHub URL")
        return value

    @model_validator(mode="after")
    def excerpt_fits_declared_bound(self) -> RuntimeJobLogEvidence:
        actual_bytes = len(self.text_excerpt.encode("utf-8"))
        if actual_bytes > 8000:
            raise ValueError("runtime job-log excerpts cannot exceed 8000 bytes")
        if self.excerpt_byte_count != actual_bytes:
            raise ValueError("excerpt_byte_count must match the retained UTF-8 excerpt")
        return self


class RuntimeArtifactObservation(StrictModel):
    schema_version: Literal["runtime_artifact_observation@1"] = (
        "runtime_artifact_observation@1"
    )
    artifact_name: str = Field(pattern=_ARTIFACT_NAME_PATTERN)
    artifact_id: int | None = Field(default=None, ge=1)
    workflow_run_id: int = Field(ge=1)
    head_sha: str = Field(pattern=_HEAD_SHA_PATTERN)
    status: RuntimeEvidenceStatus
    requirement_ids: list[str] = Field(min_length=1)
    files: list[ArtifactFileEvidence] = Field(default_factory=list)
    github_url: str | None = None
    unverified_reason: str | None = None

    @field_validator("requirement_ids")
    @classmethod
    def validate_requirement_ids(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(_REQUIREMENT_ID_PATTERN, value) for value in values):
            raise ValueError("runtime requirement IDs must use REQ-NNN")
        return _sorted_unique(values, "runtime observation requirement IDs")

    @field_validator("files")
    @classmethod
    def validate_files(
        cls, values: list[ArtifactFileEvidence]
    ) -> list[ArtifactFileEvidence]:
        paths = [value.path for value in values]
        if paths != sorted(set(paths)):
            raise ValueError("runtime artifact files must be path-sorted and unique")
        return values

    @model_validator(mode="after")
    def validate_status(self) -> RuntimeArtifactObservation:
        if self.status is RuntimeEvidenceStatus.observed:
            if self.artifact_id is None or not self.files:
                raise ValueError(
                    "observed runtime artifacts require an artifact ID and file evidence"
                )
            if self.unverified_reason is not None:
                raise ValueError(
                    "observed runtime artifacts cannot carry an unverified reason"
                )
        else:
            if not self.unverified_reason or not self.unverified_reason.strip():
                raise ValueError("unverified runtime artifacts require a reason")
            if self.files:
                raise ValueError(
                    "unverified runtime artifacts cannot carry file evidence"
                )
        return self


class RequirementRuntimeEvidence(StrictModel):
    requirement_id: str = Field(pattern=_REQUIREMENT_ID_PATTERN)
    status: RuntimeEvidenceStatus
    artifact_names: list[str] = Field(default_factory=list)
    reason: str | None = None

    @field_validator("artifact_names")
    @classmethod
    def validate_artifact_names(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(_ARTIFACT_NAME_PATTERN, value) for value in values):
            raise ValueError("invalid runtime artifact name")
        return _sorted_unique(values, "runtime evidence artifact names")

    @model_validator(mode="after")
    def validate_status(self) -> RequirementRuntimeEvidence:
        if self.status is RuntimeEvidenceStatus.observed:
            if not self.artifact_names:
                raise ValueError("observed runtime evidence requires an artifact")
            if self.reason is not None:
                raise ValueError("observed runtime evidence cannot carry a reason")
        elif not self.reason or not self.reason.strip():
            raise ValueError("unverified runtime evidence requires a reason")
        return self


class RuntimeEvidenceAssessment(StrictModel):
    schema_version: Literal["runtime_evidence_assessment@1"] = (
        "runtime_evidence_assessment@1"
    )
    head_sha: str = Field(pattern=_HEAD_SHA_PATTERN)
    external_ci_status: ExternalCIStatus
    requirements: list[RequirementRuntimeEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ordered_requirements(self) -> RuntimeEvidenceAssessment:
        requirement_ids = [item.requirement_id for item in self.requirements]
        if requirement_ids != sorted(set(requirement_ids)):
            raise ValueError("runtime evidence requirements must be sorted and unique")
        return self


class RuntimeEvidenceObservation(StrictModel):
    """Append-only runtime evidence collected for one exact pull-request head.

    Runtime evidence is deliberately nested beside, rather than folded into,
    ``external_ci_status``. The assessment copies GitHub's status as supplied;
    artifact or log presence can never promote that value.
    """

    schema_version: Literal["runtime_evidence_observation@1"] = (
        "runtime_evidence_observation@1"
    )
    observation_id: str = Field(pattern=r"^runtime_obs_[0-9a-f]{32}$")
    changeset_id: str = Field(min_length=1, max_length=200)
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    pr_number: int = Field(ge=1, le=_POSTGRES_INTEGER_MAX)
    head_sha: str = Field(pattern=_HEAD_SHA_PATTERN)
    ci_observation_id: str = Field(min_length=1, max_length=200)
    ci_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_acceptance_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    artifacts: list[RuntimeArtifactObservation] = Field(default_factory=list)
    job_logs: list[RuntimeJobLogEvidence] = Field(default_factory=list)
    assessment: RuntimeEvidenceAssessment
    collection_errors: list[str] = Field(default_factory=list)

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value: datetime) -> datetime:
        return _aware_datetime(value, "observed_at")

    @field_validator("collection_errors")
    @classmethod
    def validate_collection_errors(cls, values: list[str]) -> list[str]:
        if any(
            not value.strip()
            or len(value) > 2000
            or any(character in value for character in ("\x00", "\r"))
            for value in values
        ):
            raise ValueError("runtime collection errors must be bounded text")
        return _sorted_unique(values, "runtime collection errors")

    @model_validator(mode="after")
    def exact_head_and_unique_evidence(self) -> RuntimeEvidenceObservation:
        if self.assessment.head_sha != self.head_sha:
            raise ValueError("runtime assessment must use the observation head SHA")
        if any(item.head_sha != self.head_sha for item in self.artifacts):
            raise ValueError("runtime artifacts must use the observation head SHA")
        if any(item.head_sha != self.head_sha for item in self.job_logs):
            raise ValueError("runtime job logs must use the observation head SHA")

        artifact_keys = [
            (item.workflow_run_id, item.artifact_id, item.artifact_name)
            for item in self.artifacts
        ]
        if len(artifact_keys) != len(set(artifact_keys)):
            raise ValueError("runtime artifact identities must be unique")
        artifact_ids = [
            item.artifact_id for item in self.artifacts if item.artifact_id is not None
        ]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("GitHub runtime artifact IDs must be unique")
        if self.artifacts != sorted(
            self.artifacts,
            key=lambda item: (
                item.workflow_run_id,
                item.artifact_id or 0,
                item.artifact_name,
            ),
        ):
            raise ValueError("runtime artifacts must be deterministically sorted")

        job_keys = [(item.workflow_run_id, item.job_id) for item in self.job_logs]
        if len(job_keys) != len(set(job_keys)):
            raise ValueError("runtime job-log identities must be unique")
        job_ids = [item.job_id for item in self.job_logs]
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("GitHub runtime job IDs must be unique")
        if self.job_logs != sorted(
            self.job_logs, key=lambda item: (item.workflow_run_id, item.job_id)
        ):
            raise ValueError("runtime job logs must be deterministically sorted")

        assessed_requirements = {
            item.requirement_id for item in self.assessment.requirements
        }
        if any(
            not set(artifact.requirement_ids) <= assessed_requirements
            for artifact in self.artifacts
        ):
            raise ValueError(
                "runtime artifacts cannot reference unassessed requirements"
            )
        observed_pairs = {
            (artifact.artifact_name, requirement_id)
            for artifact in self.artifacts
            if artifact.status is RuntimeEvidenceStatus.observed
            for requirement_id in artifact.requirement_ids
        }
        for result in self.assessment.requirements:
            if not all(
                (artifact_name, result.requirement_id) in observed_pairs
                for artifact_name in result.artifact_names
            ):
                raise ValueError(
                    "runtime assessment must reference observed exact-head artifacts"
                )
        return self

    def evidence_hash(self) -> str:
        """Stable payload identity for append-only repeated-poll deduplication."""
        payload = self.model_dump(
            mode="json", exclude={"observation_id", "observed_at"}
        )
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()
