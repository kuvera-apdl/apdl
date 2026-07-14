"""Focused tests for strict evidence-backed Phase-6 semantic review."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.contracts import ContractBundle
from app.contracts.models import (
    BlockerCode,
    ContractBlocker,
    ContractEvidence,
    ContractRequest,
    ContractResolution,
    ContractSource,
    ContractSymbol,
    EvidenceSourceKind,
    LifecycleFact,
    LifecycleKind,
    RuntimeFingerprint,
    SourceProvenance,
    SymbolKind,
)
from app.inspection import DependencySlice, EvidenceKind, EvidenceRef
from app.profiling import RepoProfile
from app.profiling.models import (
    CIWorkflow,
    CommandKind,
    RepoCommand,
    TestFacility as ProfileTestFacility,
)
from app.requirements import compile_requirement_ledger
from app.semantic_review import (
    FindingCode,
    ModelResponseStatus,
    ReviewDecision,
    ReviewParseError,
    ReviewReferenceError,
    UncertaintyCode,
    assemble_review_verdict,
    build_deterministic_findings,
    build_deterministic_uncertainties,
    build_reference_index,
    parse_model_review_response,
    parse_review_verdict,
    render_semantic_review_prompt,
)
from app.semantic_review.prompt import SEMANTIC_REVIEW_DIFF_CAP
from app.verification import (
    build_verification_plan,
    evaluate_verification_coverage,
)


def _evidence(
    number: int,
    *,
    path: str = "src/change.ts",
    kind: EvidenceKind = EvidenceKind.file,
    excerpt: str | None = "export const changed = true",
    symbol: str | None = None,
    target_path: str | None = None,
    source_path: str | None = None,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=f"ev_{number:024x}",
        kind=kind,
        path=path,
        content_sha256=f"{number:064x}",
        excerpt=excerpt,
        symbol=symbol,
        target_path=target_path,
        source_path=source_path,
    )


def _profile() -> RepoProfile:
    workflow = ".github/workflows/ci.yml"
    return RepoProfile(
        commands=[
            RepoCommand(
                kind=CommandKind.test,
                command="npm test",
                cwd=".",
                source_path="package.json",
            )
        ],
        test_facilities=[
            ProfileTestFacility(
                name="vitest", package_path=".", source_path="package.json"
            )
        ],
        ci_workflows=[CIWorkflow(provider="github_actions", path=workflow)],
        protected_paths=[workflow],
    )


def _context(
    *,
    spec: str = "Preserve the observable behavior.",
    risk: str = "medium",
    dependency_slice: DependencySlice | None = None,
    contracts: ContractBundle | None = None,
    coverage_paths: list[str] | None = None,
):
    ledger = compile_requirement_ledger(
        title="Review task",
        spec=spec,
        risk=risk,
        github_check_name="ci / test",
    )
    dependency_slice = dependency_slice or DependencySlice(
        changed_files=[_evidence(1)]
    )
    contracts = contracts or ContractBundle()
    plan = build_verification_plan(ledger, _profile())
    coverage = evaluate_verification_coverage(
        plan,
        changed_paths=coverage_paths
        if coverage_paths is not None
        else ["tests/change.test.ts"],
    )
    return ledger, contracts, dependency_slice, plan, coverage


def _model_json(ledger, evidence_id: str, *, decision: str = "approved") -> str:
    instructions = [] if decision == "approved" else ["Fix the evidenced defect."]
    return json.dumps(
        {
            "schema_version": "review_model_response@1",
            "requirement_decisions": [
                {
                    "requirement_id": requirement.requirement_id,
                    "decision": decision,
                    "evidence_ids": [evidence_id],
                    "rationale": "The cited repository evidence supports this decision.",
                    "actionable_instructions": instructions,
                }
                for requirement in ledger.requirements
            ],
            "uncertainties": [],
            "actionable_instructions": instructions,
        }
    )


def _findings(context, diff_text: str):
    ledger, contracts, dependency_slice, plan, coverage = context
    return build_deterministic_findings(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=plan,
        verification_coverage=coverage,
        diff_text=diff_text,
    )


def test_clean_evidence_and_strict_model_response_can_approve_semantics():
    context = _context()
    ledger, contracts, dependency_slice, plan, coverage = context

    verdict = assemble_review_verdict(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=plan,
        verification_coverage=coverage,
        diff_text="diff --git a/src/change.ts b/src/change.ts\n+export const changed = true",
        model_response_text=_model_json(ledger, _evidence(1).evidence_id),
    )

    assert verdict.schema_version == "review_verdict@1"
    assert verdict.overall_decision is ReviewDecision.approved
    assert verdict.model_response_status is ModelResponseStatus.parsed
    assert verdict.deterministic_findings == []
    assert verdict.uncertainties == []
    assert verdict.actionable_instructions == []
    assert len(verdict.reviewed_diff_sha256) == 64


def test_parser_is_exact_json_strict_and_reference_checked():
    ledger, contracts, dependency_slice, plan, coverage = _context()
    index = build_reference_index(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=plan,
        verification_coverage=coverage,
    )
    valid = _model_json(ledger, _evidence(1).evidence_id)
    assert parse_model_review_response(valid, reference_index=index).schema_version == (
        "review_model_response@1"
    )

    with pytest.raises(ReviewParseError, match="exact JSON"):
        parse_model_review_response(f"```json\n{valid}\n```", reference_index=index)

    payload = json.loads(valid)
    payload["extra"] = True
    with pytest.raises(ReviewParseError, match="strict schema"):
        parse_model_review_response(json.dumps(payload), reference_index=index)

    payload.pop("extra")
    payload["requirement_decisions"][0]["evidence_ids"] = ["ev_ffffffffffffffffffffffff"]
    with pytest.raises(ReviewReferenceError, match="unknown evidence"):
        parse_model_review_response(json.dumps(payload), reference_index=index)

    payload["requirement_decisions"][0]["evidence_ids"] = [plan.items[0].plan_item_id]
    with pytest.raises(ReviewReferenceError, match="approval must cite repository"):
        parse_model_review_response(json.dumps(payload), reference_index=index)


def test_final_verdict_parser_rejects_unknown_fields_and_round_trips():
    ledger, contracts, dependency_slice, plan, coverage = _context()
    verdict = assemble_review_verdict(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=plan,
        verification_coverage=coverage,
        diff_text="",
        model_response_text=_model_json(ledger, _evidence(1).evidence_id),
    )
    index = build_reference_index(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=plan,
        verification_coverage=coverage,
    )
    assert parse_review_verdict(
        verdict.model_dump_json(), reference_index=index
    ) == verdict

    payload = verdict.model_dump(mode="json")
    payload["approved"] = True
    with pytest.raises(ReviewParseError, match="strict schema"):
        parse_review_verdict(json.dumps(payload), reference_index=index)


def test_changed_missing_link_is_rejected_even_when_model_approves():
    changed = _evidence(
        1,
        path="app/page.tsx",
        excerpt='export default () => <a href="/missing">Missing</a>',
    )
    link = _evidence(
        2,
        path="app/page.tsx",
        kind=EvidenceKind.link,
        excerpt='<a href="/missing">Missing</a>',
        symbol="/missing",
    )
    dependency_slice = DependencySlice(
        changed_files=[changed],
        routes_and_handlers=[link],
        unresolved_references=["app/page.tsx:1 -> /missing"],
    )
    context = _context(
        spec="Add a reachable page link.", dependency_slice=dependency_slice
    )
    ledger, contracts, _, plan, coverage = context
    diff = (
        "diff --git a/app/page.tsx b/app/page.tsx\n"
        "+++ b/app/page.tsx\n"
        '+<a href="/missing">Missing</a>\n'
    )

    verdict = assemble_review_verdict(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=plan,
        verification_coverage=coverage,
        diff_text=diff,
        model_response_text=_model_json(ledger, changed.evidence_id),
    )

    assert verdict.overall_decision is ReviewDecision.rejected
    assert verdict.requirement_decisions[0].decision is ReviewDecision.rejected
    assert FindingCode.missing_route_or_link in {
        item.code for item in verdict.deterministic_findings
    }
    assert verdict.deterministic_errors_override_model is True


def test_listener_or_timer_without_cleanup_is_found_only_with_complete_file_evidence():
    complete = DependencySlice(
        changed_files=[
            _evidence(
                1,
                excerpt="window.addEventListener('resize', update)\nexport const x = 1",
            )
        ]
    )
    diff = (
        "diff --git a/src/change.ts b/src/change.ts\n"
        "+window.addEventListener('resize', update)\n"
    )
    findings = _findings(_context(dependency_slice=complete), diff)
    assert FindingCode.missing_cleanup in {item.code for item in findings}

    incomplete = DependencySlice(changed_files=[_evidence(1, excerpt=None)])
    findings = _findings(_context(dependency_slice=incomplete), diff)
    assert FindingCode.missing_cleanup not in {item.code for item in findings}


def test_dropped_handler_prop_with_evidenced_caller_is_rejected():
    changed = _evidence(1, path="src/Button.tsx", excerpt="export const Button = () => null")
    caller = _evidence(
        2,
        path="app/page.tsx",
        kind=EvidenceKind.caller,
        excerpt="<Button onClick={save} />",
        target_path="src/Button.tsx",
    )
    dependency_slice = DependencySlice(changed_files=[changed], callers=[caller])
    diff = (
        "diff --git a/src/Button.tsx b/src/Button.tsx\n"
        "-return <button onClick={onClick}>Save</button>\n"
        "+return <button>Save</button>\n"
    )

    findings = _findings(
        _context(
            spec="Preserve the button click interaction.",
            dependency_slice=dependency_slice,
        ),
        diff,
    )

    assert FindingCode.dropped_handler_prop in {item.code for item in findings}


def test_duplicate_initialization_and_strict_schema_weakening_are_rejected():
    dependency_slice = DependencySlice(changed_files=[_evidence(1)])
    duplicate = (
        "diff --git a/src/change.ts b/src/change.ts\n"
        "+const client = new AnalyticsClient()\n"
        "+const client = new AnalyticsClient()\n"
    )
    strict = (
        "diff --git a/src/change.ts b/src/change.ts\n"
        "+const schema = z.object({ id: z.string() }).passthrough()\n"
    )

    assert FindingCode.duplicate_initialization in {
        item.code
        for item in _findings(_context(dependency_slice=dependency_slice), duplicate)
    }
    assert FindingCode.strict_schema_violation in {
        item.code
        for item in _findings(_context(dependency_slice=dependency_slice), strict)
    }


def test_exact_required_metric_absence_is_rejected_from_complete_slice():
    dependency_slice = DependencySlice(
        changed_files=[
            _evidence(1, excerpt="export function completeSignup() { return true }")
        ]
    )
    findings = _findings(
        _context(
            spec="Emit event `signup_completed` through the analytics sink.",
            dependency_slice=dependency_slice,
        ),
        "diff --git a/src/change.ts b/src/change.ts\n+export function completeSignup() {}\n",
    )

    assert FindingCode.absent_metric in {item.code for item in findings}


def _ready_contract() -> ContractBundle:
    runtime = RuntimeFingerprint(
        runtime_name="node", runtime_version="22.1.0", operating_system="linux", architecture="x64"
    )
    provenance = SourceProvenance(
        manifest_path="package.json",
        manifest_sha256="1" * 64,
        lockfile_path="package-lock.json",
        lockfile_sha256="2" * 64,
        installed_root="node_modules/widget-sdk",
        runtime=runtime,
    )
    source = ContractSource(
        source_id="3" * 64,
        kind=EvidenceSourceKind.installed_types,
        relative_path="index.d.ts",
        sha256="4" * 64,
        provenance=provenance,
    )
    evidence = ContractEvidence(
        contract_id="5" * 64,
        ecosystem="node",
        package_path=".",
        package_name="widget-sdk",
        exact_version="1.2.3",
        sources=[source],
        symbols=[
            ContractSymbol(
                qualified_name="Client",
                kind=SymbolKind.class_,
                signature="class Client",
                source_ids=[source.source_id],
            )
        ],
        lifecycle_facts=[
            LifecycleFact(
                kind=LifecycleKind.readiness,
                statement="Call `await client.ready()` before first use.",
                source_ids=[source.source_id],
            )
        ],
    )
    request = ContractRequest(
        requirement_ids=["REQ-001"],
        ecosystem="node",
        package_path=".",
        package_name="widget-sdk",
        exact_version="1.2.3",
        manifest_path="package.json",
        lockfile_path="package-lock.json",
    )
    return ContractBundle(
        resolutions=[
            ContractResolution(
                request=request,
                disposition="ready",
                evidence=evidence,
            )
        ]
    )


def test_exact_contract_readiness_fact_rejects_use_before_ready():
    contracts = _ready_contract()
    context = _context(
        spec="Integrate the exact widget SDK client.", contracts=contracts
    )
    diff = (
        "diff --git a/src/change.ts b/src/change.ts\n"
        "+import { Client } from 'widget-sdk'\n"
        "+const client = new Client()\n"
        "+client.track('opened')\n"
    )

    findings = _findings(context, diff)

    readiness = next(item for item in findings if item.code is FindingCode.async_readiness)
    assert "5" * 64 in readiness.evidence_ids
    assert "3" * 64 in readiness.evidence_ids


def test_missing_medium_risk_coverage_is_a_non_overridable_error():
    context = _context(coverage_paths=["src/change.ts"])
    ledger, contracts, dependency_slice, plan, coverage = context

    verdict = assemble_review_verdict(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=plan,
        verification_coverage=coverage,
        diff_text="",
        model_response_text=_model_json(ledger, _evidence(1).evidence_id),
    )

    assert verdict.overall_decision is ReviewDecision.rejected
    assert FindingCode.missing_verification_coverage in {
        item.code for item in verdict.deterministic_findings
    }


def test_blocked_contract_and_truncated_slice_force_unverified_not_approval():
    request = ContractRequest(
        requirement_ids=["REQ-001"],
        ecosystem="node",
        package_path=".",
        package_name="missing-sdk",
        exact_version="1.0.0",
        manifest_path="package.json",
        lockfile_path="package-lock.json",
    )
    contracts = ContractBundle(
        resolutions=[
            ContractResolution(
                request=request,
                disposition="blocked",
                blockers=[
                    ContractBlocker(
                        code=BlockerCode.install_failed,
                        package_name="missing-sdk",
                        message="Package installation failed.",
                    )
                ],
            )
        ]
    )
    dependency_slice = DependencySlice(
        changed_files=[_evidence(1)], truncated=True
    )
    context = _context(contracts=contracts, dependency_slice=dependency_slice)
    ledger, _, _, plan, coverage = context

    verdict = assemble_review_verdict(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=plan,
        verification_coverage=coverage,
        diff_text="",
        model_response_text=_model_json(ledger, _evidence(1).evidence_id),
    )

    assert verdict.overall_decision is ReviewDecision.unverified
    assert verdict.requirement_decisions[0].decision is ReviewDecision.unverified
    assert {item.code for item in verdict.uncertainties} >= {
        UncertaintyCode.contract_blocked,
        UncertaintyCode.dependency_slice_truncated,
    }


@pytest.mark.parametrize("model_response", [None, "not json"])
def test_unavailable_or_invalid_model_can_never_fail_open(model_response):
    ledger, contracts, dependency_slice, plan, coverage = _context()

    verdict = assemble_review_verdict(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=plan,
        verification_coverage=coverage,
        diff_text="",
        model_response_text=model_response,
    )

    assert verdict.overall_decision is ReviewDecision.unverified
    assert verdict.model_response_status in {
        ModelResponseStatus.unavailable,
        ModelResponseStatus.invalid,
    }
    assert verdict.actionable_instructions


def test_prompt_consumes_every_evidence_boundary_and_is_deterministic():
    ledger, contracts, dependency_slice, plan, coverage = _context(
        contracts=_ready_contract()
    )
    findings = _findings(
        (ledger, contracts, dependency_slice, plan, coverage), ""
    )
    uncertainties = build_deterministic_uncertainties(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=plan,
        verification_coverage=coverage,
    )
    args = {
        "ledger": ledger,
        "contracts": contracts,
        "dependency_slice": dependency_slice,
        "verification_plan": plan,
        "verification_coverage": coverage,
        "deterministic_findings": findings,
        "deterministic_uncertainties": uncertainties,
        "diff_text": "diff --git a/src/change.ts b/src/change.ts",
    }

    rendered = render_semantic_review_prompt(**args)

    assert rendered == render_semantic_review_prompt(**args)
    assert "requirement_ledger@1" in rendered
    assert "contract_bundle@1" in rendered
    assert "widget-sdk" in rendered
    assert "dependency_slice@1" in rendered
    assert "verification_plan@1" in rendered
    assert "verification_coverage@1" in rendered
    assert "review_model_response@1" in rendered
    assert "deterministic error is non-overridable" in rendered


def test_truncated_diff_is_explicitly_unverified_and_hash_bound():
    ledger, contracts, dependency_slice, plan, coverage = _context()
    diff = "x" * (SEMANTIC_REVIEW_DIFF_CAP + 1)

    verdict = assemble_review_verdict(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=plan,
        verification_coverage=coverage,
        diff_text=diff,
        model_response_text=_model_json(ledger, _evidence(1).evidence_id),
    )

    assert verdict.overall_decision is ReviewDecision.unverified
    assert UncertaintyCode.diff_truncated in {
        uncertainty.code for uncertainty in verdict.uncertainties
    }
    assert verdict.reviewed_diff_sha256 != "0" * 64


def test_strict_review_models_reject_unknown_fields():
    ledger, contracts, dependency_slice, plan, coverage = _context()
    verdict = assemble_review_verdict(
        ledger=ledger,
        contracts=contracts,
        dependency_slice=dependency_slice,
        verification_plan=plan,
        verification_coverage=coverage,
        diff_text="",
        model_response_text=_model_json(ledger, _evidence(1).evidence_id),
    )
    payload = verdict.requirement_decisions[0].model_dump(mode="json")
    payload["notes"] = "permissive alias"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        type(verdict.requirement_decisions[0]).model_validate(payload)
