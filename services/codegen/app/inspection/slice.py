"""Build deterministic dependency evidence around changed repository files."""

from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from pathlib import Path

from app.inspection.models import DependencySlice, EvidenceKind, EvidenceRef
from app.inspection.repository import (
    InspectedText,
    RepositoryInspector,
    TextCollection,
    evidence_ref,
)
from app.inspection.tracing import (
    ResolvedImport,
    affected_test_paths,
    discover_links,
    discover_routes,
    lockfile_paths,
    route_matches,
    trace_local_imports,
)
from app.safety.paths import canonical_changed_path


def _normalize_changed_path(path: str) -> str:
    return canonical_changed_path(path)


def _missing_text(path: str) -> InspectedText:
    marker = f"missing-or-deleted:{path}".encode()
    return InspectedText(
        path=path,
        text="",
        content_sha256=hashlib.sha256(marker).hexdigest(),
        byte_count=0,
        truncated=False,
    )


def _line_excerpt(inspected: InspectedText, line: int, *, context: int = 0) -> str:
    lines = inspected.text.splitlines()
    start = max(0, line - 1 - context)
    end = min(len(lines), line + context)
    return "\n".join(lines[start:end])[:4000]


def _head_excerpt(inspected: InspectedText, lines: int = 20) -> tuple[int, str]:
    values = inspected.text.splitlines()
    end = max(1, min(len(values), lines))
    return end, "\n".join(values[:end])[:4000]


def _dedupe(values: list[EvidenceRef]) -> list[EvidenceRef]:
    return sorted(
        {value.evidence_id: value for value in values}.values(),
        key=lambda value: (
            value.path,
            value.start_line or 0,
            value.kind.value,
            value.target_path or "",
            value.evidence_id,
        ),
    )


def _cap(values: list[EvidenceRef], maximum: int) -> tuple[list[EvidenceRef], bool]:
    values = _dedupe(values)
    return values[:maximum], len(values) > maximum


def _import_evidence(
    relation: ResolvedImport, collection: TextCollection
) -> EvidenceRef:
    target = collection.files[relation.target_path]
    end, excerpt = _head_excerpt(target)
    return evidence_ref(
        target,
        kind=EvidenceKind.local_import,
        start_line=1,
        end_line=end,
        source_path=relation.source_path,
        source_line=relation.source_line,
        symbol=relation.symbol,
        excerpt=excerpt,
    )


def _caller_evidence(
    relation: ResolvedImport, collection: TextCollection
) -> EvidenceRef:
    source = collection.files[relation.source_path]
    return evidence_ref(
        source,
        kind=EvidenceKind.caller,
        start_line=relation.source_line,
        end_line=relation.source_line,
        target_path=relation.target_path,
        symbol=relation.symbol,
        excerpt=_line_excerpt(source, relation.source_line),
    )


def _dependency_relations(
    imports: list[ResolvedImport], changed: set[str], max_depth: int
) -> list[ResolvedImport]:
    by_source: dict[str, list[ResolvedImport]] = defaultdict(list)
    for relation in imports:
        by_source[relation.source_path].append(relation)
    pending = deque((path, 0) for path in sorted(changed))
    visited = set(changed)
    selected: list[ResolvedImport] = []
    while pending:
        path, depth = pending.popleft()
        if depth >= max_depth:
            continue
        for relation in by_source.get(path, []):
            selected.append(relation)
            if relation.target_path not in visited:
                visited.add(relation.target_path)
                pending.append((relation.target_path, depth + 1))
    return selected


def build_dependency_slice(
    root: Path,
    changed_paths: list[str],
    *,
    inspector: RepositoryInspector | None = None,
    external_contracts: list[EvidenceRef] | None = None,
    max_depth: int = 2,
    max_evidence_per_group: int = 200,
) -> DependencySlice:
    """Trace a bounded, conservative slice around ``changed_paths``.

    Missing paths are retained as content-addressed deletion markers. Import
    edges are followed at most ``max_depth`` hops; caller tracing remains one
    hop so a ubiquitous utility cannot pull an entire monorepo into context.
    """
    if max_depth < 0 or max_evidence_per_group <= 0:
        raise ValueError("dependency-slice budgets must be non-negative")
    inspector = inspector or RepositoryInspector(root)
    collection = inspector.collect_texts()
    changed = {_normalize_changed_path(path) for path in changed_paths}
    imports = trace_local_imports(collection)

    changed_evidence = [
        evidence_ref(
            collection.files.get(path, _missing_text(path)), kind=EvidenceKind.file
        )
        for path in sorted(changed)
    ]

    dependency_relations = _dependency_relations(imports, changed, max_depth)
    dependency_evidence = [
        _import_evidence(relation, collection) for relation in dependency_relations
    ]
    caller_relations = [
        relation for relation in imports if relation.target_path in changed
    ]
    caller_evidence = [
        _caller_evidence(relation, collection) for relation in caller_relations
    ]

    test_paths = affected_test_paths(collection, changed, imports)
    test_evidence: list[EvidenceRef] = []
    for path in test_paths:
        inspected = collection.files.get(path)
        if inspected is None:
            continue
        test_evidence.append(evidence_ref(inspected, kind=EvidenceKind.test))

    routes = discover_routes(collection)
    links = discover_links(collection)
    caller_paths = {relation.source_path for relation in caller_relations}
    focus_paths = changed | caller_paths | set(test_paths)
    route_evidence: list[EvidenceRef] = []
    unresolved: set[str] = set()

    for route in routes:
        if route.path not in focus_paths:
            continue
        inspected = collection.files[route.path]
        route_evidence.append(
            evidence_ref(
                inspected,
                kind=EvidenceKind.route,
                start_line=route.line,
                end_line=route.line,
                symbol=route.route,
                excerpt=_line_excerpt(inspected, route.line),
            )
        )

    for link in links:
        if link.path not in focus_paths:
            continue
        matches = [
            route for route in routes if route_matches(route.route, link.destination)
        ]
        link_source = collection.files[link.path]
        if not matches:
            unresolved.add(f"{link.path}:{link.line} -> {link.destination}")
            route_evidence.append(
                evidence_ref(
                    link_source,
                    kind=EvidenceKind.link,
                    start_line=link.line,
                    end_line=link.line,
                    symbol=link.destination,
                    excerpt=_line_excerpt(link_source, link.line),
                )
            )
            continue
        for route in matches:
            route_source = collection.files[route.path]
            route_evidence.extend(
                [
                    evidence_ref(
                        link_source,
                        kind=EvidenceKind.link,
                        start_line=link.line,
                        end_line=link.line,
                        target_path=route.path,
                        symbol=link.destination,
                        excerpt=_line_excerpt(link_source, link.line),
                    ),
                    evidence_ref(
                        route_source,
                        kind=EvidenceKind.route,
                        start_line=route.line,
                        end_line=route.line,
                        source_path=link.path,
                        source_line=link.line,
                        symbol=route.route,
                        excerpt=_line_excerpt(route_source, route.line),
                    ),
                ]
            )

    lockfile_evidence = [
        evidence_ref(collection.files[path], kind=EvidenceKind.lockfile)
        for path in lockfile_paths(collection)
    ]

    changed_evidence, changed_truncated = _cap(changed_evidence, max_evidence_per_group)
    dependency_evidence, dependency_truncated = _cap(
        dependency_evidence, max_evidence_per_group
    )
    caller_evidence, caller_truncated = _cap(caller_evidence, max_evidence_per_group)
    route_evidence, routes_truncated = _cap(route_evidence, max_evidence_per_group)
    test_evidence, tests_truncated = _cap(test_evidence, max_evidence_per_group)
    lockfile_evidence, lockfiles_truncated = _cap(
        lockfile_evidence, max_evidence_per_group
    )
    contracts, contracts_truncated = _cap(
        external_contracts or [], max_evidence_per_group
    )

    return DependencySlice(
        changed_files=changed_evidence,
        imported_local_symbols=dependency_evidence,
        callers=caller_evidence,
        routes_and_handlers=route_evidence,
        affected_tests=test_evidence,
        relevant_lockfiles=lockfile_evidence,
        external_contracts=contracts,
        unresolved_references=sorted(unresolved),
        truncated=(
            collection.truncated
            or changed_truncated
            or dependency_truncated
            or caller_truncated
            or routes_truncated
            or tests_truncated
            or lockfiles_truncated
            or contracts_truncated
        ),
    )
