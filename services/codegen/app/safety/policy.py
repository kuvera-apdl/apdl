"""Strict authority-separated safety policy contracts for Codegen.

Tenant connection preferences may only tighten the platform blast-radius
ceilings and add protected paths.  They are never interpreted directly by the
pre-push gates.  Trusted service code resolves them with the operator-owned
platform policy and passes the resulting :class:`EffectiveCodegenSafetyPolicy`
to every gate invocation.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.runtime.models import (
    RUNTIME_ACCEPTANCE_WORKFLOW_PATH,
    RuntimeAcceptanceRequest,
)

TENANT_POLICY_SCHEMA_VERSION = "tenant_codegen_connection_policy@1"
PLATFORM_POLICY_SCHEMA_VERSION = "platform_codegen_safety_policy@1"
EFFECTIVE_POLICY_SCHEMA_VERSION = "effective_codegen_safety_policy@1"
MAX_ADDITIONAL_PROTECTED_PATHS = 64

# These protections are part of the application safety floor.  An operator can
# add to them, but neither a platform policy file nor tenant input can remove
# them.
DEFAULT_PROTECTED_PATTERNS: tuple[str, ...] = (
    ".github/workflows/*",
    "*.pem",
    "*.key",
    "id_rsa*",
    ".env",
    ".env.*",
)

_DEFAULT_MAX_FILES = 50
_DEFAULT_MAX_LINES = 2000
_SHA256_PATTERN = r"^[0-9a-f]{64}$"

PositiveStrictInt = Annotated[StrictInt, Field(ge=1)]


class _StrictPolicyModel(BaseModel):
    """Base contract used for every safety-policy authority boundary."""

    model_config = ConfigDict(extra="forbid", strict=True)


def _canonical_protected_patterns(values: list[str]) -> list[str]:
    """Validate repository-relative glob patterns and return canonical order."""
    for value in values:
        if not value:
            raise ValueError("protected path patterns must not be empty")
        if len(value) > 256:
            raise ValueError("protected path patterns must be at most 256 characters")
        if value.startswith(("/", "./")) or "\\" in value:
            raise ValueError(
                "protected path patterns must be canonical repository-relative paths"
            )
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError("protected path patterns contain invalid characters")
        if ".." in value.split("/"):
            raise ValueError("protected path patterns must not traverse parent paths")
    return sorted(set(values))


class TenantCodegenGatesPolicy(_StrictPolicyModel):
    """Tenant-owned gate preferences, limited to tightening and additions."""

    max_files: PositiveStrictInt | None = None
    max_lines: PositiveStrictInt | None = None
    additional_protected_paths: list[StrictStr] = Field(
        default_factory=list,
        max_length=MAX_ADDITIONAL_PROTECTED_PATHS,
    )

    @field_validator("additional_protected_paths")
    @classmethod
    def validate_additional_protected_paths(cls, values: list[str]) -> list[str]:
        return _canonical_protected_patterns(values)


class TenantCodegenConnectionPolicy(_StrictPolicyModel):
    """Canonical tenant-owned preferences stored with a Codegen connection."""

    schema_version: Literal["tenant_codegen_connection_policy@1"] = (
        TENANT_POLICY_SCHEMA_VERSION
    )
    test_cmd: StrictStr | None = Field(default=None, min_length=1, max_length=1000)
    gates: TenantCodegenGatesPolicy = Field(
        default_factory=TenantCodegenGatesPolicy
    )
    runtime_acceptance: RuntimeAcceptanceRequest = Field(
        default_factory=RuntimeAcceptanceRequest
    )

    @field_validator("test_cmd")
    @classmethod
    def validate_test_cmd(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("test_cmd must not be blank")
            if any(character in value for character in ("\x00", "\r", "\n")):
                raise ValueError("test_cmd must be a single-line command")
        return value


class PlatformCodegenSafetyPolicy(_StrictPolicyModel):
    """Operator-owned Codegen safety ceilings and capability grants."""

    schema_version: Literal["platform_codegen_safety_policy@1"] = (
        PLATFORM_POLICY_SCHEMA_VERSION
    )
    max_files: PositiveStrictInt = _DEFAULT_MAX_FILES
    max_lines: PositiveStrictInt = _DEFAULT_MAX_LINES
    additional_protected_paths: list[StrictStr] = Field(
        default_factory=list,
        max_length=MAX_ADDITIONAL_PROTECTED_PATHS,
    )
    runtime_workflow_generation_enabled: StrictBool = False

    @field_validator("additional_protected_paths")
    @classmethod
    def validate_additional_protected_paths(cls, values: list[str]) -> list[str]:
        return _canonical_protected_patterns(values)


class EffectiveCodegenSafetyPolicy(_StrictPolicyModel):
    """Trusted resolved policy consumed by sandbox and service-side gates."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["effective_codegen_safety_policy@1"] = (
        EFFECTIVE_POLICY_SCHEMA_VERSION
    )
    max_files: PositiveStrictInt = _DEFAULT_MAX_FILES
    max_lines: PositiveStrictInt = _DEFAULT_MAX_LINES
    protected_paths: tuple[StrictStr, ...] = Field(
        default=tuple(sorted(DEFAULT_PROTECTED_PATTERNS)),
        max_length=(2 * MAX_ADDITIONAL_PROTECTED_PATHS + len(DEFAULT_PROTECTED_PATTERNS)),
    )
    runtime_workflow_generation_enabled: StrictBool = False

    @model_validator(mode="after")
    def validate_protected_paths(self) -> EffectiveCodegenSafetyPolicy:
        values = list(self.protected_paths)
        canonical = tuple(_canonical_protected_patterns(values))
        if self.protected_paths != canonical:
            raise ValueError("effective protected_paths must be sorted and unique")
        missing_defaults = set(DEFAULT_PROTECTED_PATTERNS).difference(values)
        if missing_defaults:
            raise ValueError(
                "effective protected_paths must include every built-in protection"
            )
        return self

    def canonical_digest(self) -> str:
        """Return the SHA-256 of the canonical effective-policy JSON."""
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class VerifiedProtectedPathExemption(_StrictPolicyModel):
    """Trusted proof allowing the one APDL-owned generated workflow path.

    This type is intentionally separate from every tenant-controlled model and
    cannot represent an arbitrary path.  Callers construct it only after
    deterministic runtime-workflow attestation succeeds.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["verified_protected_path_exemption@1"] = (
        "verified_protected_path_exemption@1"
    )
    kind: Literal["generated_runtime_workflow"] = "generated_runtime_workflow"
    path: Literal[RUNTIME_ACCEPTANCE_WORKFLOW_PATH] = (
        RUNTIME_ACCEPTANCE_WORKFLOW_PATH
    )
    content_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)
    runtime_acceptance_plan_sha256: StrictStr = Field(pattern=_SHA256_PATTERN)


def validate_tenant_policy_against_platform(
    tenant_policy: TenantCodegenConnectionPolicy,
    platform_policy: PlatformCodegenSafetyPolicy,
) -> None:
    """Reject tenant ceilings that purport to exceed operator-owned ceilings.

    Execution still uses :func:`resolve_effective_policy`'s ``min`` semantics,
    so a permissive legacy/corrupt stored value cannot weaken the effective
    policy.  This validation gives new API writes a clear 422-style error.
    """
    violations: list[str] = []
    tenant_max_files = tenant_policy.gates.max_files
    if tenant_max_files is not None and tenant_max_files > platform_policy.max_files:
        violations.append(
            "tenant gates.max_files cannot exceed the platform max_files ceiling "
            f"of {platform_policy.max_files}"
        )
    tenant_max_lines = tenant_policy.gates.max_lines
    if tenant_max_lines is not None and tenant_max_lines > platform_policy.max_lines:
        violations.append(
            "tenant gates.max_lines cannot exceed the platform max_lines ceiling "
            f"of {platform_policy.max_lines}"
        )
    if violations:
        raise ValueError("; ".join(violations))


def resolve_effective_policy(
    tenant_policy: TenantCodegenConnectionPolicy,
    platform_policy: PlatformCodegenSafetyPolicy | None = None,
) -> EffectiveCodegenSafetyPolicy:
    """Resolve tenant preferences against operator policy with union/min rules."""
    platform = platform_policy or load_platform_safety_policy()
    tenant_gates = tenant_policy.gates
    max_files = min(
        platform.max_files,
        tenant_gates.max_files
        if tenant_gates.max_files is not None
        else platform.max_files,
    )
    max_lines = min(
        platform.max_lines,
        tenant_gates.max_lines
        if tenant_gates.max_lines is not None
        else platform.max_lines,
    )
    protected_paths = tuple(
        sorted(
            set(DEFAULT_PROTECTED_PATTERNS)
            | set(platform.additional_protected_paths)
            | set(tenant_gates.additional_protected_paths)
        )
    )
    return EffectiveCodegenSafetyPolicy(
        max_files=max_files,
        max_lines=max_lines,
        protected_paths=protected_paths,
        runtime_workflow_generation_enabled=(
            platform.runtime_workflow_generation_enabled
            and tenant_policy.runtime_acceptance.enabled
        ),
    )


def load_platform_safety_policy(
    path: str | os.PathLike[str] | None = None,
) -> PlatformCodegenSafetyPolicy:
    """Load the optional operator policy JSON, failing closed on bad config.

    With no explicit ``path``, ``CODEGEN_PLATFORM_SAFETY_POLICY_PATH`` is read.
    An unset environment variable selects the safe built-in policy.  Configured
    paths must be absolute so service startup cannot depend on its working
    directory.
    """
    configured_path = (
        os.fspath(path)
        if path is not None
        else os.getenv("CODEGEN_PLATFORM_SAFETY_POLICY_PATH", "").strip()
    )
    if not configured_path:
        return PlatformCodegenSafetyPolicy()
    if not os.path.isabs(configured_path):
        raise RuntimeError(
            "CODEGEN_PLATFORM_SAFETY_POLICY_PATH must be an absolute path"
        )

    try:
        raw = Path(configured_path).read_text(encoding="utf-8")
        return PlatformCodegenSafetyPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(
            "CODEGEN_PLATFORM_SAFETY_POLICY_PATH must contain a valid strict "
            "platform_codegen_safety_policy@1 JSON object"
        ) from exc


__all__ = [
    "DEFAULT_PROTECTED_PATTERNS",
    "EFFECTIVE_POLICY_SCHEMA_VERSION",
    "EffectiveCodegenSafetyPolicy",
    "MAX_ADDITIONAL_PROTECTED_PATHS",
    "PLATFORM_POLICY_SCHEMA_VERSION",
    "PlatformCodegenSafetyPolicy",
    "TENANT_POLICY_SCHEMA_VERSION",
    "TenantCodegenConnectionPolicy",
    "TenantCodegenGatesPolicy",
    "VerifiedProtectedPathExemption",
    "load_platform_safety_policy",
    "resolve_effective_policy",
    "validate_tenant_policy_against_platform",
]
