"""Factories for strict provider-free repository preparation evidence."""

from __future__ import annotations

from app.contracts.models import ContractBundle
from app.editor.base import EditRequest
from app.editor.worker_contract import (
    decode_codegen_preparation_request,
    encode_codegen_preparation_request,
)
from app.inspection.models import InspectionSnapshot
from app.inspection.preflight import RepositoryPreflightAttestation
from app.inspection.preparation import RepositoryPreparationEvidence
from app.profiling.models import RepoProfile
from app.requirements import compile_requirement_ledger
from app.runtime.planner import build_runtime_acceptance_plan
from app.verification import build_verification_plan


def repository_preparation(
    request: EditRequest,
    *,
    head_sha: str = "a" * 40,
    tree_sha: str = "b" * 40,
    file_count: int = 3,
    contract_bundle: ContractBundle | None = None,
) -> RepositoryPreparationEvidence:
    """Build internally consistent evidence bound to ``request``."""
    source = decode_codegen_preparation_request(
        encode_codegen_preparation_request(request)
    )
    profile = RepoProfile(repo=request.repo, branch=request.branch)
    ledger = request.requirement_ledger or compile_requirement_ledger(
        title=request.title,
        spec=request.spec,
        constraints=request.constraints,
        risk=request.risk_level,
        verification_command=request.test_cmd,
    )
    verification_plan = build_verification_plan(ledger, profile)
    runtime_plan = build_runtime_acceptance_plan(
        profile,
        verification_plan,
        policy=request.runtime_acceptance_policy,
    )
    return RepositoryPreparationEvidence(
        request_sha256=source.request_sha256(),
        target_branch=request.branch,
        attestation=RepositoryPreflightAttestation(
            repository=request.repo,
            source_branch=(
                request.branch if request.existing_branch else request.base_branch
            ),
            head_sha=head_sha,
            tree_sha=tree_sha,
            file_count=file_count,
        ),
        repo_profile=profile,
        verification_command=request.test_cmd,
        has_test_runner=False,
        verification_preamble="Provider-free repository preparation completed.",
        requirement_ledger=ledger,
        inspection_snapshot=InspectionSnapshot(),
        verification_plan=verification_plan,
        runtime_acceptance_plan=runtime_plan,
        contract_bundle=contract_bundle or ContractBundle(),
    )
