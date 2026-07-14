"""Strict, versioned contracts for exact dependency evidence.

The models in this module are pipeline boundaries.  They deliberately reject
unknown fields and keep a hard distinction between an installed-package fact,
a compile-checked example, and a blocker.  None of these records represents a
GitHub CI result.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _stable_unique(values: list[str]) -> list[str]:
    return sorted({value.strip() for value in values if value.strip()})


class EvidenceSourceKind(str, Enum):
    installed_metadata = "installed_metadata"
    installed_types = "installed_types"
    installed_exports = "installed_exports"
    bundled_documentation = "bundled_documentation"
    installed_implementation = "installed_implementation"


class SymbolKind(str, Enum):
    function = "function"
    async_function = "async_function"
    class_ = "class"
    interface = "interface"
    type_alias = "type_alias"
    constant = "constant"
    module_export = "module_export"
    method = "method"


class LifecycleKind(str, Enum):
    initialization = "initialization"
    readiness = "readiness"
    asynchronous = "asynchronous"
    singleton = "singleton"
    cleanup = "cleanup"
    error = "error"


class BlockerCode(str, Enum):
    missing_manifest = "missing_manifest"
    missing_lockfile = "missing_lockfile"
    conflicting_lockfiles = "conflicting_lockfiles"
    unresolved_version = "unresolved_version"
    version_mismatch = "version_mismatch"
    unsupported_ecosystem = "unsupported_ecosystem"
    unsupported_toolchain = "unsupported_toolchain"
    install_failed = "install_failed"
    package_not_found = "package_not_found"
    symbol_not_found = "symbol_not_found"
    inspection_failed = "inspection_failed"
    compile_check_unavailable = "compile_check_unavailable"
    example_check_failed = "example_check_failed"
    budget_exceeded = "budget_exceeded"


class RuntimeFingerprint(StrictModel):
    schema_version: Literal["runtime_fingerprint@1"] = "runtime_fingerprint@1"
    runtime_name: str = Field(min_length=1)
    runtime_version: str = Field(min_length=1)
    operating_system: str = Field(min_length=1)
    architecture: str = Field(min_length=1)


class ContractRequest(StrictModel):
    schema_version: Literal["contract_request@1"] = "contract_request@1"
    requirement_ids: list[str] = Field(default_factory=list)
    ecosystem: str = Field(min_length=1)
    package_path: str = Field(min_length=1)
    package_name: str = Field(min_length=1)
    exact_version: str | None = None
    manifest_path: str = Field(min_length=1)
    lockfile_path: str | None = None
    symbols: list[str] = Field(default_factory=list)

    @field_validator("requirement_ids", "symbols")
    @classmethod
    def stable_lists(cls, value: list[str]) -> list[str]:
        return _stable_unique(value)


class ContractCacheIdentity(StrictModel):
    schema_version: Literal["contract_cache_identity@1"] = "contract_cache_identity@1"
    project_scope: str = Field(min_length=1)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    ecosystem: str = Field(min_length=1)
    package_path: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lockfile_path: str = Field(min_length=1)
    lockfile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime: RuntimeFingerprint
    extractor_version: str = Field(min_length=1)
    selection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_key: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceProvenance(StrictModel):
    schema_version: Literal["contract_provenance@1"] = "contract_provenance@1"
    manifest_path: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lockfile_path: str = Field(min_length=1)
    lockfile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installed_root: str = Field(min_length=1)
    runtime: RuntimeFingerprint


class ContractSource(StrictModel):
    schema_version: Literal["contract_source@1"] = "contract_source@1"
    source_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: EvidenceSourceKind
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: SourceProvenance


class ContractSymbol(StrictModel):
    schema_version: Literal["contract_symbol@1"] = "contract_symbol@1"
    qualified_name: str = Field(min_length=1)
    kind: SymbolKind
    signature: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)

    @field_validator("source_ids")
    @classmethod
    def stable_sources(cls, value: list[str]) -> list[str]:
        return _stable_unique(value)


class LifecycleFact(StrictModel):
    schema_version: Literal["lifecycle_fact@1"] = "lifecycle_fact@1"
    kind: LifecycleKind
    statement: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)

    @field_validator("source_ids")
    @classmethod
    def stable_sources(cls, value: list[str]) -> list[str]:
        return _stable_unique(value)


class CompileCheckedExample(StrictModel):
    schema_version: Literal["compile_checked_example@1"] = "compile_checked_example@1"
    language: str = Field(min_length=1)
    snippet: str = Field(min_length=1)
    command: str = Field(min_length=1)
    tool_version: str = Field(min_length=1)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ids: list[str] = Field(min_length=1)
    check_result: Literal["passed"] = "passed"

    @field_validator("source_ids")
    @classmethod
    def stable_sources(cls, value: list[str]) -> list[str]:
        return _stable_unique(value)


class ContractEvidence(StrictModel):
    schema_version: Literal["contract_evidence@1"] = "contract_evidence@1"
    contract_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    ecosystem: str = Field(min_length=1)
    package_path: str = Field(min_length=1)
    package_name: str = Field(min_length=1)
    exact_version: str = Field(min_length=1)
    sources: list[ContractSource] = Field(min_length=1)
    symbols: list[ContractSymbol] = Field(default_factory=list)
    lifecycle_facts: list[LifecycleFact] = Field(default_factory=list)
    examples: list[CompileCheckedExample] = Field(default_factory=list)

    @model_validator(mode="after")
    def references_existing_sources(self) -> ContractEvidence:
        known = {source.source_id for source in self.sources}
        used = {
            source_id
            for item in [*self.symbols, *self.lifecycle_facts, *self.examples]
            for source_id in item.source_ids
        }
        missing = sorted(used - known)
        if missing:
            raise ValueError(
                f"Unknown contract source references: {', '.join(missing)}"
            )
        return self


class ContractBlocker(StrictModel):
    schema_version: Literal["contract_blocker@1"] = "contract_blocker@1"
    code: BlockerCode
    severity: Literal["warning", "blocking"] = "blocking"
    package_name: str = Field(min_length=1)
    message: str = Field(min_length=1)
    paths: list[str] = Field(default_factory=list)

    @field_validator("paths")
    @classmethod
    def stable_paths(cls, value: list[str]) -> list[str]:
        return _stable_unique(value)


class ContractInstallRequest(StrictModel):
    schema_version: Literal["contract_install_request@1"] = "contract_install_request@1"
    repository_root: str = Field(min_length=1)
    request: ContractRequest
    runtime: RuntimeFingerprint


class ContractInstallResult(StrictModel):
    schema_version: Literal["contract_install_result@1"] = "contract_install_result@1"
    status: Literal["installed", "failed", "unsupported"]
    installed_root: str | None = None
    message: str | None = None

    @model_validator(mode="after")
    def valid_status_payload(self) -> ContractInstallResult:
        if self.status == "installed" and not self.installed_root:
            raise ValueError("installed_root is required for an installed result")
        if self.status != "installed" and not self.message:
            raise ValueError("message is required for a failed/unsupported result")
        return self


class ContractCheckRequest(StrictModel):
    schema_version: Literal["contract_check_request@1"] = "contract_check_request@1"
    ecosystem: str = Field(min_length=1)
    package_name: str = Field(min_length=1)
    exact_version: str = Field(min_length=1)
    installed_root: str = Field(min_length=1)
    language: str = Field(min_length=1)
    snippet: str = Field(min_length=1)


class ContractCheckResult(StrictModel):
    schema_version: Literal["contract_check_result@1"] = "contract_check_result@1"
    passed: bool
    command: str = Field(min_length=1)
    tool_version: str = Field(min_length=1)
    output: str = ""


class ContractResolution(StrictModel):
    schema_version: Literal["contract_resolution@1"] = "contract_resolution@1"
    request: ContractRequest
    cache_identity: ContractCacheIdentity | None = None
    disposition: Literal["ready", "blocked"]
    evidence: ContractEvidence | None = None
    blockers: list[ContractBlocker] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_disposition(self) -> ContractResolution:
        blocking = any(item.severity == "blocking" for item in self.blockers)
        if self.disposition == "ready" and (self.evidence is None or blocking):
            raise ValueError(
                "ready resolution requires evidence and no blocking blocker"
            )
        if self.disposition == "blocked" and not blocking:
            raise ValueError("blocked resolution requires a blocking blocker")
        if self.evidence is not None:
            if self.evidence.package_name != self.request.package_name:
                raise ValueError("evidence package does not match the request")
            if (
                self.request.exact_version is not None
                and self.evidence.exact_version != self.request.exact_version
            ):
                raise ValueError("evidence version does not match the request")
        return self


class ContractBundle(StrictModel):
    schema_version: Literal["contract_bundle@1"] = "contract_bundle@1"
    resolutions: list[ContractResolution] = Field(default_factory=list)
