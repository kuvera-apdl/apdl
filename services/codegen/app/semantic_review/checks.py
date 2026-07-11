"""Conservative deterministic semantic checks over bounded review evidence."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from app.contracts import ContractBundle
from app.contracts.models import LifecycleKind
from app.inspection import DependencySlice, EvidenceKind, EvidenceRef
from app.requirements import ImplementationStatus, Requirement, RequirementLedger
from app.semantic_review.models import (
    DeterministicFinding,
    FindingCode,
    FindingSeverity,
)
from app.verification import (
    CoverageDisposition,
    VerificationCoverage,
    VerificationPlan,
)


@dataclass(frozen=True)
class _DiffFile:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


@dataclass(frozen=True)
class _FindingDraft:
    code: FindingCode
    severity: FindingSeverity
    requirement_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    message: str
    instruction: str
    sort_path: str = ""


def _parse_diff(diff_text: str) -> dict[str, _DiffFile]:
    added: dict[str, list[str]] = defaultdict(list)
    removed: dict[str, list[str]] = defaultdict(list)
    current = ""
    for line in diff_text.splitlines():
        match = re.match(r"^diff --git a/(.+) b/(.+)$", line)
        if match:
            current = match.group(2)
            continue
        if line.startswith("+++ b/"):
            current = line[6:]
            continue
        if not current or line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            added[current].append(line[1:])
        elif line.startswith("-"):
            removed[current].append(line[1:])
    return {
        path: _DiffFile(tuple(added[path]), tuple(removed[path]))
        for path in sorted(set(added) | set(removed))
    }


def _active(ledger: RequirementLedger) -> list[Requirement]:
    return [
        item
        for item in ledger.requirements
        if item.implementation_status
        not in {ImplementationStatus.blocked, ImplementationStatus.descoped}
    ]


def _ids(requirements: list[Requirement]) -> tuple[str, ...]:
    return tuple(item.requirement_id for item in requirements)


def _evidence_by_path(dependency_slice: DependencySlice) -> dict[str, list[EvidenceRef]]:
    result: dict[str, list[EvidenceRef]] = defaultdict(list)
    for group_name in (
        "changed_files",
        "imported_local_symbols",
        "callers",
        "routes_and_handlers",
        "affected_tests",
        "external_contracts",
    ):
        for evidence in getattr(dependency_slice, group_name):
            result[evidence.path].append(evidence)
    return result


def _full_changed_evidence(dependency_slice: DependencySlice) -> list[EvidenceRef]:
    return [
        item
        for item in dependency_slice.changed_files
        if item.excerpt is not None
        and not item.truncated
        and item.start_line is None
        and item.end_line is None
    ]


def _path_evidence_ids(
    by_path: dict[str, list[EvidenceRef]], path: str
) -> tuple[str, ...]:
    return tuple(sorted({item.evidence_id for item in by_path.get(path, [])}))


def _missing_routes(
    *,
    active: list[Requirement],
    dependency_slice: DependencySlice,
    diff: dict[str, _DiffFile],
    by_path: dict[str, list[EvidenceRef]],
) -> list[_FindingDraft]:
    findings = []
    changed_paths = {item.path for item in dependency_slice.changed_files}
    for unresolved in dependency_slice.unresolved_references:
        match = re.match(r"^(.*):[0-9]+ -> (.+)$", unresolved)
        if match is None:
            continue
        path, destination = match.groups()
        if path not in changed_paths or destination not in "\n".join(diff.get(path, _DiffFile()).added):
            continue
        link_ids = tuple(
            sorted(
                item.evidence_id
                for item in dependency_slice.routes_and_handlers
                if item.kind is EvidenceKind.link
                and item.path == path
                and item.symbol == destination
                and item.target_path is None
            )
        ) or _path_evidence_ids(by_path, path)
        if not link_ids:
            continue
        findings.append(
            _FindingDraft(
                FindingCode.missing_route_or_link,
                FindingSeverity.error,
                _ids(active),
                link_ids,
                f"Changed link {destination!r} in {path} has no resolved route.",
                f"Create and register the real {destination!r} route or remove the link.",
                path,
            )
        )
    return findings


def _missing_cleanup(
    *,
    active: list[Requirement],
    dependency_slice: DependencySlice,
    diff: dict[str, _DiffFile],
) -> list[_FindingDraft]:
    pairs = (
        ("addEventListener(", "removeEventListener("),
        ("setInterval(", "clearInterval("),
        ("setTimeout(", "clearTimeout("),
    )
    findings = []
    for evidence in _full_changed_evidence(dependency_slice):
        added = "\n".join(diff.get(evidence.path, _DiffFile()).added)
        body = evidence.excerpt or ""
        for setup, cleanup in pairs:
            if setup in added and setup in body and cleanup not in body:
                findings.append(
                    _FindingDraft(
                        FindingCode.missing_cleanup,
                        FindingSeverity.error,
                        _ids(active),
                        (evidence.evidence_id,),
                        f"{evidence.path} adds {setup[:-1]} without {cleanup[:-1]} cleanup.",
                        f"Add lifecycle cleanup with {cleanup[:-1]} for the created resource.",
                        evidence.path,
                    )
                )
    return findings


def _dropped_handlers(
    *,
    active: list[Requirement],
    dependency_slice: DependencySlice,
    diff: dict[str, _DiffFile],
    by_path: dict[str, list[EvidenceRef]],
) -> list[_FindingDraft]:
    findings = []
    requirement_text = " ".join(item.observable_behavior for item in active).lower()
    for path, change in diff.items():
        removed_handlers = {
            match.group(1)
            for line in change.removed
            for match in [re.search(r"\b(on[A-Z][A-Za-z0-9_]*)\s*=", line)]
            if match is not None
        }
        for handler in sorted(removed_handlers):
            if any(re.search(rf"\b{re.escape(handler)}\b", line) for line in change.added):
                continue
            caller_ids = tuple(
                item.evidence_id
                for item in dependency_slice.callers
                if item.excerpt and handler in item.excerpt
            )
            behavior_requires_handler = any(
                marker in requirement_text
                for marker in (handler.lower(), "interaction", "click", "handler")
            )
            evidence_ids = tuple(
                sorted({*_path_evidence_ids(by_path, path), *caller_ids})
            )
            if not evidence_ids or not (caller_ids or behavior_requires_handler):
                continue
            findings.append(
                _FindingDraft(
                    FindingCode.dropped_handler_prop,
                    FindingSeverity.error,
                    _ids(active),
                    evidence_ids,
                    f"{path} removes forwarding for handler prop {handler}.",
                    f"Restore {handler} forwarding or update every evidenced caller and requirement.",
                    path,
                )
            )
    return findings


_INITIALIZER = re.compile(
    r"(?:\bnew\s+[A-Z][A-Za-z0-9_]*(?:Client|SDK|Analytics)\s*\("
    r"|\b(?:create|initialize|init)[A-Z_][A-Za-z0-9_]*\s*\()"
)


def _duplicate_initialization(
    *,
    active: list[Requirement],
    diff: dict[str, _DiffFile],
    by_path: dict[str, list[EvidenceRef]],
) -> list[_FindingDraft]:
    findings = []
    for path, change in diff.items():
        initializers = [
            re.sub(r"\s+", " ", line.strip())
            for line in change.added
            if _INITIALIZER.search(line)
        ]
        for expression, count in sorted(Counter(initializers).items()):
            evidence_ids = _path_evidence_ids(by_path, path)
            if count < 2 or not evidence_ids:
                continue
            findings.append(
                _FindingDraft(
                    FindingCode.duplicate_initialization,
                    FindingSeverity.error,
                    _ids(active),
                    evidence_ids,
                    f"{path} adds the same initialization {count} times: {expression}",
                    "Create one lifecycle-owned instance and reuse it at the evidenced call sites.",
                    path,
                )
            )
    return findings


_PERMISSIVE_SCHEMA = re.compile(
    r"(?:extra\s*=\s*['\"](?:allow|ignore)['\"]|\.passthrough\(\)"
    r"|additionalProperties\s*[:=]\s*true)",
    re.IGNORECASE,
)


def _strict_schema_violations(
    *,
    active: list[Requirement],
    diff: dict[str, _DiffFile],
    by_path: dict[str, list[EvidenceRef]],
) -> list[_FindingDraft]:
    findings = []
    for path, change in diff.items():
        reasons = [line.strip() for line in change.added if _PERMISSIVE_SCHEMA.search(line)]
        if any(".strict()" in line for line in change.removed) and not any(
            ".strict()" in line for line in change.added
        ):
            reasons.append("removed .strict() without a replacement")
        evidence_ids = _path_evidence_ids(by_path, path)
        if not reasons or not evidence_ids:
            continue
        findings.append(
            _FindingDraft(
                FindingCode.strict_schema_violation,
                FindingSeverity.error,
                _ids(active),
                evidence_ids,
                f"{path} weakens a canonical strict schema: {'; '.join(reasons)}",
                "Restore extra-field rejection and one canonical field shape; do not add aliases.",
                path,
            )
        )
    return findings


def _absent_metrics(
    *,
    active: list[Requirement],
    dependency_slice: DependencySlice,
    diff: dict[str, _DiffFile],
) -> list[_FindingDraft]:
    if dependency_slice.truncated or not dependency_slice.changed_files:
        return []
    full_changed = _full_changed_evidence(dependency_slice)
    if len(full_changed) != len(dependency_slice.changed_files):
        return []
    context_evidence = [
        *full_changed,
        *dependency_slice.imported_local_symbols,
        *dependency_slice.callers,
    ]
    corpus = "\n".join(item.excerpt or "" for item in context_evidence)
    corpus += "\n" + "\n".join(
        line for change in diff.values() for line in change.added
    )
    findings = []
    for requirement in active:
        text = f"{requirement.original_source_text}\n{requirement.observable_behavior}"
        if not re.search(r"\b(?:event|metric|tracking|exposure|analytics)\b", text, re.I):
            continue
        tokens = re.findall(
            r"\b(?:event|metric|exposure)\s+[`'\"]([^`'\"]+)[`'\"]",
            text,
            re.IGNORECASE,
        )
        for token in sorted(set(tokens)):
            if token in corpus:
                continue
            evidence_ids = tuple(item.evidence_id for item in full_changed)
            findings.append(
                _FindingDraft(
                    FindingCode.absent_metric,
                    FindingSeverity.error,
                    (requirement.requirement_id,),
                    evidence_ids,
                    f"The exact required metric/event {token!r} is absent from the complete changed-file slice.",
                    f"Emit {token!r} through the repository's real analytics sink and add coverage.",
                    requirement.requirement_id,
                )
            )
    return findings


def _async_readiness(
    *,
    active: list[Requirement],
    contracts: ContractBundle,
    diff: dict[str, _DiffFile],
) -> list[_FindingDraft]:
    added = "\n".join(line for change in diff.values() for line in change.added)
    lower_added = added.lower()
    findings = []
    active_ids = set(_ids(active))
    for resolution in contracts.resolutions:
        evidence = resolution.evidence
        if evidence is None:
            continue
        readiness = [
            fact
            for fact in evidence.lifecycle_facts
            if fact.kind in {LifecycleKind.readiness, LifecycleKind.asynchronous}
        ]
        if not readiness:
            continue
        usage_markers = {evidence.package_name.lower()}
        usage_markers.update(
            symbol.qualified_name.rsplit(".", 1)[-1].lower()
            for symbol in evidence.symbols
        )
        if not any(marker and marker in lower_added for marker in usage_markers):
            continue
        readiness_tokens: set[str] = set()
        requires_await = False
        for fact in readiness:
            requires_await = requires_await or bool(
                re.search(r"\bawait\b", fact.statement, re.IGNORECASE)
            )
            readiness_tokens.update(
                match.group(0).lower()
                for match in re.finditer(
                    r"\b(?:ready|connect|initialize|start|wait)[A-Za-z0-9_]*\b",
                    fact.statement,
                    re.IGNORECASE,
                )
            )
        # A generic "asynchronous" fact without an explicit await or readiness
        # operation does not prove a missing step, so leave it to model review.
        if not requires_await and not readiness_tokens:
            continue
        await_satisfied = not requires_await or "await" in lower_added
        operation_satisfied = not readiness_tokens or any(
            token in lower_added for token in readiness_tokens
        )
        if await_satisfied and operation_satisfied:
            continue
        requirement_ids = tuple(
            value for value in resolution.request.requirement_ids if value in active_ids
        ) or _ids(active)
        evidence_ids = tuple(
            sorted(
                {
                    evidence.contract_id,
                    *(source_id for fact in readiness for source_id in fact.source_ids),
                }
            )
        )
        findings.append(
            _FindingDraft(
                FindingCode.async_readiness,
                FindingSeverity.error,
                requirement_ids,
                evidence_ids,
                f"The diff uses {evidence.package_name}@{evidence.exact_version} without its evidenced asynchronous readiness step.",
                "Follow the exact installed lifecycle evidence and await readiness before first use.",
                evidence.package_name,
            )
        )
    return findings


def _contract_and_coverage_findings(
    *,
    active: list[Requirement],
    contracts: ContractBundle,
    verification_plan: VerificationPlan,
    verification_coverage: VerificationCoverage,
) -> list[_FindingDraft]:
    findings = []
    available_contracts = {
        resolution.evidence.contract_id
        for resolution in contracts.resolutions
        if resolution.evidence is not None
    }
    plan_ids_by_requirement: dict[str, tuple[str, ...]] = {
        requirement.requirement_id: tuple(
            item.plan_item_id
            for item in verification_plan.items
            if item.requirement_id == requirement.requirement_id
        )
        for requirement in active
    }
    for requirement in active:
        missing = sorted(
            set(requirement.required_contract_evidence_ids) - available_contracts
        )
        if not missing:
            continue
        evidence_ids = plan_ids_by_requirement[requirement.requirement_id] or tuple(
            item.evidence_id for item in requirement.expected_ci_evidence
        )
        findings.append(
            _FindingDraft(
                FindingCode.missing_contract_evidence,
                FindingSeverity.error,
                (requirement.requirement_id,),
                evidence_ids,
                "Required exact dependency contracts are missing: " + ", ".join(missing),
                "Resolve the exact installed dependency contracts before approval.",
                requirement.requirement_id,
            )
        )

    if verification_coverage.disposition is CoverageDisposition.missing_required_coverage:
        findings.append(
            _FindingDraft(
                FindingCode.missing_verification_coverage,
                FindingSeverity.error,
                _ids(active),
                tuple(item.plan_item_id for item in verification_plan.items),
                verification_coverage.disposition_reason,
                "Add the required risk-policy tests without weakening existing checks.",
            )
        )
    if (
        verification_coverage.disposition
        is CoverageDisposition.rejected_workflow_gate_relaxation
    ):
        findings.append(
            _FindingDraft(
                FindingCode.workflow_gate_relaxation,
                FindingSeverity.error,
                _ids(active),
                tuple(item.plan_item_id for item in verification_plan.items),
                verification_coverage.disposition_reason,
                "Restore every protected workflow gate; only preserve or strengthen it.",
            )
        )
    return findings


def build_deterministic_findings(
    *,
    ledger: RequirementLedger,
    contracts: ContractBundle,
    dependency_slice: DependencySlice,
    verification_plan: VerificationPlan,
    verification_coverage: VerificationCoverage,
    diff_text: str,
) -> list[DeterministicFinding]:
    """Return stable findings only when supplied structure proves the pattern."""
    active = _active(ledger)
    if not active:
        return []
    diff = _parse_diff(diff_text)
    by_path = _evidence_by_path(dependency_slice)
    drafts = [
        *_missing_routes(
            active=active,
            dependency_slice=dependency_slice,
            diff=diff,
            by_path=by_path,
        ),
        *_missing_cleanup(active=active, dependency_slice=dependency_slice, diff=diff),
        *_dropped_handlers(
            active=active,
            dependency_slice=dependency_slice,
            diff=diff,
            by_path=by_path,
        ),
        *_duplicate_initialization(active=active, diff=diff, by_path=by_path),
        *_strict_schema_violations(active=active, diff=diff, by_path=by_path),
        *_absent_metrics(active=active, dependency_slice=dependency_slice, diff=diff),
        *_async_readiness(active=active, contracts=contracts, diff=diff),
        *_contract_and_coverage_findings(
            active=active,
            contracts=contracts,
            verification_plan=verification_plan,
            verification_coverage=verification_coverage,
        ),
    ]
    drafts.sort(
        key=lambda item: (
            item.code.value,
            item.sort_path,
            item.message,
            item.evidence_ids,
        )
    )
    return [
        DeterministicFinding(
            finding_id=f"RF-{index:03d}",
            code=draft.code,
            severity=draft.severity,
            requirement_ids=list(draft.requirement_ids),
            evidence_ids=list(draft.evidence_ids),
            message=draft.message,
            actionable_instruction=draft.instruction,
        )
        for index, draft in enumerate(drafts, start=1)
    ]
