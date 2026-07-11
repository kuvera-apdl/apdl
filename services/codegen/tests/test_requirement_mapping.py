"""Requirement-to-contract and changed-file evidence mapping."""

from app.contracts.models import (
    ContractBundle,
    ContractEvidence,
    ContractRequest,
    ContractResolution,
    ContractSource,
    EvidenceSourceKind,
    RuntimeFingerprint,
    SourceProvenance,
)
from app.requirements import (
    ImplementationStatus,
    bind_contract_evidence,
    compile_requirement_ledger,
    map_implementation_evidence,
)


def _bundle() -> ContractBundle:
    runtime = RuntimeFingerprint(
        runtime_name="python", runtime_version="3.12", operating_system="linux", architecture="x86_64"
    )
    provenance = SourceProvenance(
        manifest_path="package.json",
        manifest_sha256="a" * 64,
        lockfile_path="package-lock.json",
        lockfile_sha256="b" * 64,
        installed_root="node_modules",
        runtime=runtime,
    )
    evidence = ContractEvidence(
        contract_id="c" * 64,
        ecosystem="node",
        package_path=".",
        package_name="analytics-sdk",
        exact_version="1.2.3",
        sources=[
            ContractSource(
                source_id="d" * 64,
                kind=EvidenceSourceKind.installed_types,
                relative_path="node_modules/analytics-sdk/index.d.ts",
                sha256="e" * 64,
                provenance=provenance,
            )
        ],
    )
    request = ContractRequest(
        requirement_ids=["REQ-001"],
        ecosystem="node",
        package_path=".",
        package_name="analytics-sdk",
        exact_version="1.2.3",
        manifest_path="package.json",
        lockfile_path="package-lock.json",
    )
    return ContractBundle(
        resolutions=[
            ContractResolution(
                request=request, disposition="ready", evidence=evidence
            )
        ]
    )


def test_exact_contract_is_bound_only_to_named_requirement():
    ledger = compile_requirement_ledger(
        title="Track signup",
        spec="Use analytics-sdk to track signup.",
        constraints=["Do not change the login flow."],
    )
    bound = bind_contract_evidence(ledger, _bundle())
    assert bound.requirements[0].required_contract_evidence_ids == ["c" * 64]
    assert bound.requirements[1].required_contract_evidence_ids == []


def test_every_active_requirement_maps_to_changed_files_without_claiming_ci():
    ledger = compile_requirement_ledger(
        title="Track signup",
        spec="Track signup from the analytics handler.",
        constraints=["Keep authentication unchanged."],
    )
    mapped = map_implementation_evidence(
        ledger, ["app/analytics/handler.ts", "tests/signup.test.ts"]
    )
    assert mapped.ready_for_pull_request()
    assert all(
        item.implementation_status is ImplementationStatus.implemented
        for item in mapped.requirements
    )
    assert all(item.expected_ci_evidence for item in mapped.requirements)


def test_no_changed_files_cannot_finalize_a_ledger():
    ledger = compile_requirement_ledger(title="T", spec="Do the thing.")
    try:
        map_implementation_evidence(ledger, [])
    except ValueError as exc:
        assert "changed path" in str(exc)
    else:
        raise AssertionError("empty implementation evidence should be rejected")
