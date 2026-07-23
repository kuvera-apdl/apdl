"""Provider-free repository preparation and tree-bound editing evidence."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from app import config
from app.contracts.cache import FilesystemContractCache
from app.contracts.image_checker import PYRIGHT_VERSION, TYPESCRIPT_VERSION
from app.contracts.installer import ImageOwnedCheckRunner, SandboxedInstallRunner
from app.contracts.models import (
    ContractBundle,
    ContractRequest,
    RuntimeFingerprint,
)
from app.contracts.resolver import resolve_contracts
from app.contracts.selection import select_contract_requests
from app.editor.base import EditRequest
from app.inspection.models import InspectionSnapshot, StrictModel
from app.inspection.preflight import (
    RepositoryPreflightAttestation,
    attest_repository_checkout,
)
from app.inspection.repository import RepositoryInspector
from app.profiling import profile_repository
from app.profiling.models import CommandKind, RepoProfile
from app.requirements import bind_contract_evidence, compile_requirement_ledger
from app.requirements.models import ImplementationStatus, RequirementLedger
from app.runtime.models import RuntimeAcceptancePlan
from app.runtime.planner import build_runtime_acceptance_plan
from app.verification import VerificationPlan, build_verification_plan


class RepositoryPreparationError(RuntimeError):
    """The provider-free phase could not produce complete trusted evidence."""


class _StrictPreparationModel(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _runtime_input_sha256(value: RepoProfile | VerificationPlan) -> str:
    encoded = json.dumps(
        value.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class RepositoryPreparationEvidence(_StrictPreparationModel):
    """Strict evidence consumed by the later provider-bearing editor phase."""

    schema_version: Literal["repository_preparation_evidence@1"] = (
        "repository_preparation_evidence@1"
    )
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_branch: str = Field(min_length=1, max_length=255)
    attestation: RepositoryPreflightAttestation
    repo_profile: RepoProfile
    verification_command: str | None = None
    has_test_runner: bool
    verification_preamble: str
    requirement_ledger: RequirementLedger
    inspection_snapshot: InspectionSnapshot
    verification_plan: VerificationPlan
    runtime_acceptance_plan: RuntimeAcceptancePlan
    contract_bundle: ContractBundle

    @model_validator(mode="after")
    def evidence_is_internally_bound(self) -> RepositoryPreparationEvidence:
        if (
            self.repo_profile.repo != self.attestation.repository
            or self.repo_profile.branch != self.target_branch
        ):
            raise ValueError("prepared repository profile identity does not match")
        if (
            self.verification_plan.source_ledger_sha256
            != self.requirement_ledger.source_sha256
            or self.runtime_acceptance_plan.source_ledger_sha256
            != self.requirement_ledger.source_sha256
        ):
            raise ValueError("prepared plans do not match the requirement ledger")
        expected_profile_sha256 = _runtime_input_sha256(self.repo_profile)
        expected_verification_sha256 = _runtime_input_sha256(self.verification_plan)
        if (
            self.runtime_acceptance_plan.repo_profile_sha256
            != expected_profile_sha256
            or self.runtime_acceptance_plan.verification_plan_sha256
            != expected_verification_sha256
            or self.runtime_acceptance_plan.repo != self.repo_profile.repo
            or self.runtime_acceptance_plan.branch != self.repo_profile.branch
        ):
            raise ValueError("prepared runtime plan provenance does not match")
        for resolution in self.contract_bundle.resolutions:
            identity = resolution.cache_identity
            if identity is not None and identity.repository != self.attestation.repository:
                raise ValueError(
                    "prepared dependency contract repository does not match"
                )
        return self


class RepositoryPreparationSuccess(_StrictPreparationModel):
    """Canonical successful output from the provider-free phase."""

    schema_version: Literal["repository_preparation_success@1"] = (
        "repository_preparation_success@1"
    )
    success: Literal[True] = True
    preparation: RepositoryPreparationEvidence


class RepositoryPreparationFailure(_StrictPreparationModel):
    """Canonical sanitized failure output from the provider-free phase."""

    schema_version: Literal["repository_preparation_failure@1"] = (
        "repository_preparation_failure@1"
    )
    success: Literal[False] = False
    error: str = Field(min_length=1, max_length=256)


def _profile_verify_command(profile: RepoProfile) -> str | None:
    for cwd in [".", *sorted({command.cwd for command in profile.commands})]:
        commands = [
            command.command
            for kind in (CommandKind.typecheck, CommandKind.build, CommandKind.test)
            for command in profile.commands
            if command.cwd == cwd and command.kind is kind
        ]
        if commands:
            return " && ".join(dict.fromkeys(commands))
    return None


def _verification_preamble(
    *, has_test_runner: bool, verification_command: str | None, profile: RepoProfile
) -> str:
    lines = ["## Repository verification context (read before writing code)"]
    if verification_command:
        lines.append(
            f"Your change is gated on this command passing: "
            f"`{verification_command}`. It runs in GitHub CI as the authoritative "
            "type/build/test evidence. Make sure everything you add passes it."
        )
    else:
        lines.append(
            "No automated verification command was detected for this repo. Keep "
            "the change minimal and self-contained."
        )
    if has_test_runner:
        lines.append(
            "This repo HAS a test framework. Add a test that exercises the new "
            "behavior, using the framework the repo ALREADY depends on."
        )
    else:
        lines.append(
            "This repo has NO test framework configured. Do NOT add test files or "
            "import a test library that the repository does not already depend on."
        )
    if profile.uncertainties:
        lines.append(
            "Repository profiler uncertainties: "
            + ", ".join(
                sorted({uncertainty.code.value for uncertainty in profile.uncertainties})
            )
        )
    return "\n".join(lines)


def _inspection_for_ledger(
    repository_root: Path, ledger: RequirementLedger
) -> InspectionSnapshot:
    inspector = RepositoryInspector(repository_root)
    snapshot = inspector.snapshot()
    stopwords = {
        "acceptance",
        "change",
        "existing",
        "implement",
        "requirement",
        "should",
        "tests",
        "that",
        "this",
        "with",
    }
    candidates: list[str] = []
    for requirement in ledger.requirements:
        for token in re.findall(
            r"[A-Za-z_][A-Za-z0-9_.:/-]{3,}", requirement.observable_behavior
        ):
            normalized = token.strip(".,:;()[]{}")
            if normalized.casefold() not in stopwords:
                candidates.append(normalized)
    evidence = list(snapshot.evidence)
    for token in list(dict.fromkeys(candidates))[:12]:
        evidence.extend(
            inspector.search(token, case_sensitive=False, max_results=8)
        )
    evidence = sorted(
        {item.evidence_id: item for item in evidence}.values(),
        key=lambda item: (item.path, item.start_line or 0, item.evidence_id),
    )
    return InspectionSnapshot(
        evidence=evidence,
        skipped_paths=snapshot.skipped_paths,
        bytes_inspected=snapshot.bytes_inspected,
        truncated=snapshot.truncated,
    )


def _contract_runtime() -> RuntimeFingerprint:
    versions = [
        f"python={platform.python_version()}",
        f"pyright={PYRIGHT_VERSION}",
        f"typescript={TYPESCRIPT_VERSION}",
    ]
    for executable in ("node", "npm", "uv"):
        try:
            result = subprocess.run(
                [executable, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin"},
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout.strip():
            versions.append(f"{executable}={result.stdout.strip()}")
    return RuntimeFingerprint(
        runtime_name="apdl-codegen-preparation-toolchains",
        runtime_version=";".join(versions),
        operating_system=platform.system().lower() or "unknown",
        architecture=platform.machine().lower() or "unknown",
    )


def contract_requests_for_ledger(
    profile: RepoProfile, ledger: RequirementLedger
) -> list[ContractRequest]:
    selected: dict[tuple[str, str, str, str | None], ContractRequest] = {}
    for requirement in ledger.requirements:
        if requirement.implementation_status in {
            ImplementationStatus.blocked,
            ImplementationStatus.descoped,
        }:
            continue
        source = "\n".join(
            [
                requirement.original_source_text,
                requirement.observable_behavior,
                requirement.implementable_scope,
            ]
        )
        for request in select_contract_requests(
            profile,
            source,
            requirement_ids=[requirement.requirement_id],
        ):
            key = (
                request.ecosystem,
                request.package_path,
                request.package_name,
                request.exact_version,
            )
            previous = selected.get(key)
            if previous is None:
                selected[key] = request
                continue
            payload = previous.model_dump(mode="json")
            payload["requirement_ids"] = sorted(
                {*previous.requirement_ids, *request.requirement_ids}
            )
            selected[key] = ContractRequest.model_validate(payload)
    return [selected[key] for key in sorted(selected)]


def prepare_repository(
    repository_root: Path,
    request: EditRequest,
    *,
    request_sha256: str,
    workdir_base: Path,
) -> RepositoryPreparationEvidence:
    """Build all install/check-derived evidence without provider credentials."""
    source_branch = request.branch if request.existing_branch else request.base_branch
    attestation = attest_repository_checkout(
        repository_root,
        repository=request.repo,
        source_branch=source_branch,
    )
    profile = profile_repository(repository_root).model_copy(
        update={"repo": request.repo, "branch": request.branch}
    )
    verification_command = request.test_cmd or _profile_verify_command(profile)
    has_test_runner = bool(profile.test_facilities) or any(
        command.kind is CommandKind.test for command in profile.commands
    )
    ledger = request.requirement_ledger or compile_requirement_ledger(
        title=request.title,
        spec=request.spec,
        constraints=request.constraints,
        risk=request.risk_level,
        verification_command=verification_command,
    )
    active = [
        item
        for item in ledger.requirements
        if item.implementation_status
        not in {ImplementationStatus.blocked, ImplementationStatus.descoped}
    ]
    if not active:
        raise RepositoryPreparationError(
            "repository preparation requires an active requirement"
        )
    inspection = _inspection_for_ledger(repository_root, ledger)
    verification_plan = build_verification_plan(ledger, profile)
    runtime_plan = build_runtime_acceptance_plan(
        profile,
        verification_plan,
        policy=request.runtime_acceptance_policy,
    )
    contracts = ContractBundle()
    if config.codegen_contracts_enabled():
        requests = contract_requests_for_ledger(profile, ledger)
        contracts = resolve_contracts(
            repository_root,
            project_scope=request.project_scope or request.repo,
            repository=request.repo,
            requests=requests,
            runtime=_contract_runtime(),
            install_runner=SandboxedInstallRunner(
                sandboxed=True,
                timeout_seconds=config.codegen_contract_install_timeout(),
                workdir_base=workdir_base,
            ),
            check_runner=ImageOwnedCheckRunner(
                sandboxed=True,
                workdir_base=workdir_base,
            ),
            cache=FilesystemContractCache(
                Path(config.codegen_contract_cache_dir())
            ),
        )
        blocked = [
            resolution
            for resolution in contracts.resolutions
            if resolution.disposition == "blocked"
        ]
        if blocked:
            raise RepositoryPreparationError(
                "exact dependency contract preparation was blocked"
            )
        ledger = bind_contract_evidence(ledger, contracts)
        verification_plan = build_verification_plan(ledger, profile)
        runtime_plan = build_runtime_acceptance_plan(
            profile,
            verification_plan,
            policy=request.runtime_acceptance_policy,
        )
    return RepositoryPreparationEvidence(
        request_sha256=request_sha256,
        target_branch=request.branch,
        attestation=attestation,
        repo_profile=profile,
        verification_command=verification_command,
        has_test_runner=has_test_runner,
        verification_preamble=_verification_preamble(
            has_test_runner=has_test_runner,
            verification_command=verification_command,
            profile=profile,
        ),
        requirement_ledger=ledger,
        inspection_snapshot=inspection,
        verification_plan=verification_plan,
        runtime_acceptance_plan=runtime_plan,
        contract_bundle=contracts,
    )
