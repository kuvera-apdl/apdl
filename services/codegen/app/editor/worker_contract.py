"""Strict stdin contract between the Codegen controller and editor worker."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Annotated, BinaryIO, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    model_validator,
)

from app.editor.base import EditRequest
from app.inspection.preflight import RepositoryPreflightAttestation
from app.requirements.models import RequirementLedger
from app.runtime.models import RuntimeAcceptancePlan, RuntimeAcceptancePolicy
from app.safety.policy import EffectiveCodegenSafetyPolicy

CODEGEN_WORKER_REQUEST_SCHEMA_VERSION = "codegen_worker_request@1"
MAX_CODEGEN_WORKER_REQUEST_BYTES = 1024 * 1024

# The source validation happens before the provider-free inspection container is
# launched. Reserve more than the maximum serialized preflight attestation so an
# input accepted there cannot cross the final envelope's global byte limit.
_PREFLIGHT_ATTESTATION_RESERVE_BYTES = 4096
_REPOSITORY_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")

_OptionalBoundedString = Annotated[StrictStr, Field(min_length=1, max_length=1000)]
_Constraint = Annotated[StrictStr, Field(max_length=8192)]


class CodegenWorkerRequestError(ValueError):
    """Stable rejection raised before a worker can invoke a provider."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _CodegenWorkerRequestSource(_StrictModel):
    """Controller-owned request fields validated before any container launch."""

    read_token: StrictStr = Field(min_length=1, max_length=4096)
    repository: StrictStr = Field(
        min_length=3,
        max_length=256,
        pattern=_REPOSITORY_PATTERN,
    )
    project_scope: StrictStr = Field(min_length=1, max_length=256)
    base_branch: StrictStr = Field(min_length=1, max_length=255)
    branch: StrictStr = Field(min_length=1, max_length=255)
    title: StrictStr = Field(min_length=1, max_length=200)
    spec: StrictStr = Field(min_length=1, max_length=256 * 1024)
    constraints: list[_Constraint] = Field(max_length=100)
    test_cmd: _OptionalBoundedString | None
    safety_policy: EffectiveCodegenSafetyPolicy
    safety_policy_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    revert_sha: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None
    existing_branch: StrictBool
    expected_head_sha: Annotated[
        StrictStr,
        Field(pattern=r"^[0-9a-f]{40}$"),
    ] | None
    risk_level: Literal["low", "medium", "high"]
    requirement_ledger: RequirementLedger | None
    runtime_acceptance_plan: RuntimeAcceptancePlan | None
    runtime_acceptance_policy: RuntimeAcceptancePolicy

    @model_validator(mode="after")
    def validate_identity_and_policy(self) -> _CodegenWorkerRequestSource:
        for name, branch in (
            ("base_branch", self.base_branch),
            ("branch", self.branch),
        ):
            if (
                not _BRANCH_PATTERN.fullmatch(branch)
                or branch.endswith(("/", "."))
                or ".." in branch
                or "@{" in branch
                or branch.startswith("-")
            ):
                raise ValueError(f"{name} is not a canonical Git branch name")
        if self.safety_policy.canonical_digest() != self.safety_policy_sha256:
            raise ValueError("effective safety policy digest does not match its payload")
        return self


class CodegenWorkerRequest(_CodegenWorkerRequestSource):
    """Complete and sole task-bearing input accepted by the editor worker."""

    schema_version: Literal["codegen_worker_request@1"]
    repository_preflight: RepositoryPreflightAttestation

    @model_validator(mode="after")
    def validate_preflight_identity(self) -> CodegenWorkerRequest:
        expected_source = self.branch if self.existing_branch else self.base_branch
        if (
            self.repository_preflight.repository != self.repository
            or self.repository_preflight.source_branch != expected_source
        ):
            raise ValueError("repository preflight identity does not match worker request")
        return self

    def to_edit_request(self) -> EditRequest:
        """Reconstruct the in-process editor request after strict validation."""
        return EditRequest(
            repo=self.repository,
            project_scope=self.project_scope,
            base_branch=self.base_branch,
            branch=self.branch,
            token=self.read_token,
            title=self.title,
            spec=self.spec,
            constraints=list(self.constraints),
            test_cmd=self.test_cmd,
            safety_policy=self.safety_policy,
            revert_sha=self.revert_sha,
            existing_branch=self.existing_branch,
            expected_head_sha=self.expected_head_sha,
            risk_level=self.risk_level,
            requirement_ledger=self.requirement_ledger,
            runtime_acceptance_plan=self.runtime_acceptance_plan,
            repository_preflight=self.repository_preflight,
            runtime_acceptance_policy=self.runtime_acceptance_policy,
        )


def _source_values(request: EditRequest) -> dict[str, object]:
    return {
        "read_token": request.token,
        "repository": request.repo,
        "project_scope": request.project_scope or request.repo,
        "base_branch": request.base_branch,
        "branch": request.branch,
        "title": request.title,
        "spec": request.spec,
        "constraints": request.constraints,
        "test_cmd": request.test_cmd,
        "safety_policy": request.safety_policy,
        "safety_policy_sha256": request.safety_policy.canonical_digest(),
        "revert_sha": request.revert_sha,
        "existing_branch": request.existing_branch,
        "expected_head_sha": request.expected_head_sha,
        "risk_level": request.risk_level,
        "requirement_ledger": request.requirement_ledger,
        "runtime_acceptance_plan": request.runtime_acceptance_plan,
        "runtime_acceptance_policy": request.runtime_acceptance_policy,
    }


def validate_codegen_worker_request_source(request: EditRequest) -> None:
    """Reject malformed or oversized task data before the first container runs."""
    try:
        source = _CodegenWorkerRequestSource.model_validate(_source_values(request))
    except ValidationError as exc:
        raise CodegenWorkerRequestError(
            "codegen worker request source violates the strict schema"
        ) from exc
    serialized = json.dumps(
        source.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(serialized) > (
        MAX_CODEGEN_WORKER_REQUEST_BYTES - _PREFLIGHT_ATTESTATION_RESERVE_BYTES
    ):
        raise CodegenWorkerRequestError("codegen worker request exceeds its input limit")


def encode_codegen_worker_request(request: EditRequest) -> bytes:
    """Serialize one canonical worker request, enforcing the global byte bound."""
    if request.repository_preflight is None:
        raise CodegenWorkerRequestError(
            "codegen worker request requires repository preflight evidence"
        )
    try:
        envelope = CodegenWorkerRequest.model_validate(
            {
                **_source_values(request),
                "schema_version": CODEGEN_WORKER_REQUEST_SCHEMA_VERSION,
                "repository_preflight": request.repository_preflight,
            }
        )
    except ValidationError as exc:
        raise CodegenWorkerRequestError(
            "codegen worker request violates the strict schema"
        ) from exc
    serialized = envelope.model_dump_json().encode("utf-8")
    if len(serialized) > MAX_CODEGEN_WORKER_REQUEST_BYTES:
        raise CodegenWorkerRequestError("codegen worker request exceeds its input limit")
    return serialized


def _parse_strict_json_object(raw: str) -> Mapping[str, object]:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_nonfinite(_value: str) -> None:
        raise ValueError("non-finite JSON value")

    value = json.loads(
        raw,
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_nonfinite,
    )
    if not isinstance(value, dict):
        raise ValueError("worker request must be a JSON object")
    return value


def decode_codegen_worker_request(raw: bytes) -> CodegenWorkerRequest:
    """Decode one strict UTF-8 envelope without reflecting attacker input."""
    if len(raw) > MAX_CODEGEN_WORKER_REQUEST_BYTES:
        raise CodegenWorkerRequestError("codegen worker request exceeds its input limit")
    try:
        decoded = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CodegenWorkerRequestError(
            "codegen worker request must be UTF-8 JSON"
        ) from exc
    try:
        _parse_strict_json_object(decoded)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CodegenWorkerRequestError(
            "codegen worker request is not a strict JSON object"
        ) from exc
    try:
        return CodegenWorkerRequest.model_validate_json(decoded)
    except ValidationError as exc:
        raise CodegenWorkerRequestError(
            "codegen worker request violates the strict schema"
        ) from exc


def read_codegen_worker_request(stream: BinaryIO) -> CodegenWorkerRequest:
    """Read no more than one byte beyond the accepted stdin request limit."""
    try:
        raw = stream.read(MAX_CODEGEN_WORKER_REQUEST_BYTES + 1)
    except OSError as exc:
        raise CodegenWorkerRequestError(
            "codegen worker request could not be read"
        ) from exc
    return decode_codegen_worker_request(raw)
