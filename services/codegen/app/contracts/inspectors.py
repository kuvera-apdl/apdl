"""Static installed-tree inspectors for Node and Python dependencies.

Inspection never imports or executes the installed package.  Package metadata,
declaration files, stubs, and Python ASTs are the only evidence sources.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Iterable
from email.parser import Parser
from pathlib import Path, PurePosixPath

from app.contracts.cache import canonical_sha256, sha256_bytes
from app.contracts.models import (
    BlockerCode,
    CompileCheckedExample,
    ContractBlocker,
    ContractCacheIdentity,
    ContractCheckRequest,
    ContractCheckResult,
    ContractCheckStatus,
    ContractEvidence,
    ContractRequest,
    ContractSource,
    ContractSymbol,
    EvidenceSourceKind,
    LifecycleFact,
    LifecycleKind,
    SourceProvenance,
    SymbolKind,
)


CheckRunner = Callable[[ContractCheckRequest], ContractCheckResult]

_MAX_EVIDENCE_FILE_BYTES = 1_000_000
_MAX_TYPE_FILES = 20
_MAX_SYMBOLS = 200
_NODE_NAME = re.compile(r"^(?:@[A-Za-z0-9._-]+/[A-Za-z0-9._-]+|[A-Za-z0-9._-]+)$")


def _blocker(
    request: ContractRequest,
    code: BlockerCode,
    message: str,
    *paths: str,
    severity: str = "blocking",
) -> ContractBlocker:
    return ContractBlocker(
        code=code,
        severity=severity,
        package_name=request.package_name,
        message=message,
        paths=list(paths),
    )


def _safe_child(root: Path, relative: str) -> Path | None:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    candidate = root.joinpath(*pure.parts)
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def _read_evidence(path: Path) -> tuple[bytes | None, bool]:
    try:
        if path.stat().st_size > _MAX_EVIDENCE_FILE_BYTES:
            return None, True
        return path.read_bytes(), False
    except OSError:
        return None, False


def _provenance(
    identity: ContractCacheIdentity, installed_label: str
) -> SourceProvenance:
    return SourceProvenance(
        manifest_path=identity.manifest_path,
        manifest_sha256=identity.manifest_sha256,
        lockfile_path=identity.lockfile_path,
        lockfile_sha256=identity.lockfile_sha256,
        installed_root=installed_label,
        runtime=identity.runtime,
    )


def _source(
    *,
    path: Path,
    installed_root: Path,
    content: bytes,
    kind: EvidenceSourceKind,
    provenance: SourceProvenance,
) -> ContractSource:
    relative = path.relative_to(installed_root).as_posix()
    digest = sha256_bytes(content)
    source_id = canonical_sha256(
        {"kind": kind.value, "relative_path": relative, "sha256": digest}
    )
    return ContractSource(
        source_id=source_id,
        kind=kind,
        relative_path=relative,
        sha256=digest,
        provenance=provenance,
    )


def _example(
    *,
    request: ContractRequest,
    installed_root: Path,
    language: str,
    snippet: str,
    source_ids: list[str],
    check_runner: CheckRunner | None,
) -> tuple[CompileCheckedExample | None, ContractBlocker | None]:
    if check_runner is None:
        return None, _blocker(
            request,
            BlockerCode.compile_check_unavailable,
            "No compile-check runner was supplied; no usage example was emitted.",
        )
    result = check_runner(
        ContractCheckRequest(
            ecosystem=request.ecosystem,
            package_name=request.package_name,
            exact_version=request.exact_version or "unknown",
            installed_root=installed_root.as_posix(),
            language=language,
            snippet=snippet,
        )
    )
    if result.status is ContractCheckStatus.unavailable:
        return None, _blocker(
            request,
            BlockerCode.compile_check_unavailable,
            "The injected compile checker was unavailable; no usage example "
            "was emitted.",
        )
    if result.status is ContractCheckStatus.failed:
        return None, _blocker(
            request,
            BlockerCode.example_check_failed,
            "The candidate usage example did not pass the injected checker.",
            severity="warning",
        )
    if result.status is not ContractCheckStatus.passed:
        raise ValueError(f"unsupported contract check status: {result.status}")
    return (
        CompileCheckedExample(
            language=language,
            snippet=snippet,
            command=result.command,
            tool_version=result.tool_version,
            output_sha256=sha256_bytes(result.output.encode("utf-8")),
            source_ids=source_ids,
        ),
        None,
    )


def _finish_evidence(
    request: ContractRequest,
    sources: list[ContractSource],
    symbols: list[ContractSymbol],
    lifecycle: list[LifecycleFact],
    examples: list[CompileCheckedExample],
) -> ContractEvidence:
    sources = sorted(sources, key=lambda item: (item.relative_path, item.kind.value))
    symbols = sorted(
        {item.qualified_name: item for item in symbols}.values(),
        key=lambda item: (item.qualified_name, item.kind.value),
    )[:_MAX_SYMBOLS]
    lifecycle = sorted(
        lifecycle, key=lambda item: (item.kind.value, item.statement, item.source_ids)
    )
    payload = {
        "ecosystem": request.ecosystem,
        "package_path": request.package_path,
        "package_name": request.package_name,
        "exact_version": request.exact_version,
        "sources": [item.model_dump(mode="json") for item in sources],
        "symbols": [item.model_dump(mode="json") for item in symbols],
        "lifecycle_facts": [item.model_dump(mode="json") for item in lifecycle],
        "examples": [item.model_dump(mode="json") for item in examples],
    }
    return ContractEvidence(contract_id=canonical_sha256(payload), **payload)


def _node_type_paths(package_root: Path, metadata: dict) -> list[Path]:
    candidates: set[Path] = set()
    for key in ("types", "typings"):
        value = metadata.get(key)
        if isinstance(value, str):
            path = _safe_child(package_root, value)
            if path is not None and path.is_file():
                candidates.add(path)

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "types" and isinstance(child, str):
                    path = _safe_child(package_root, child)
                    if path is not None and path.is_file():
                        candidates.add(path)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(metadata.get("exports"))
    index = package_root / "index.d.ts"
    if not candidates and index.is_file():
        candidates.add(index)
    return sorted(candidates)[:_MAX_TYPE_FILES]


def _node_symbols(text: str, source_id: str) -> list[ContractSymbol]:
    symbols: list[ContractSymbol] = []
    patterns: tuple[tuple[re.Pattern[str], SymbolKind], ...] = (
        (
            re.compile(
                r"(?ms)^\s*export\s+(?:declare\s+)?(?:async\s+)?function\s+"
                r"(?P<name>[A-Za-z_$][\w$]*)\s*(?P<tail>[^;{]*(?:;|\{))"
            ),
            SymbolKind.function,
        ),
        (
            re.compile(
                r"(?ms)^\s*export\s+(?:declare\s+)?class\s+"
                r"(?P<name>[A-Za-z_$][\w$]*)(?P<tail>[^\{]*\{)"
            ),
            SymbolKind.class_,
        ),
        (
            re.compile(
                r"(?ms)^\s*export\s+(?:declare\s+)?interface\s+"
                r"(?P<name>[A-Za-z_$][\w$]*)(?P<tail>[^\{]*\{)"
            ),
            SymbolKind.interface,
        ),
        (
            re.compile(
                r"(?ms)^\s*export\s+(?:declare\s+)?type\s+"
                r"(?P<name>[A-Za-z_$][\w$]*)(?P<tail>[^;]*;)"
            ),
            SymbolKind.type_alias,
        ),
        (
            re.compile(
                r"(?ms)^\s*export\s+(?:declare\s+)?(?:const|let|var)\s+"
                r"(?P<name>[A-Za-z_$][\w$]*)(?P<tail>[^;]*;)"
            ),
            SymbolKind.constant,
        ),
    )
    for pattern, kind in patterns:
        for match in pattern.finditer(text):
            signature = " ".join(match.group(0).strip().split())
            actual_kind = (
                SymbolKind.async_function
                if kind is SymbolKind.function
                and re.search(r"\basync\s+function\b", signature)
                else kind
            )
            symbols.append(
                ContractSymbol(
                    qualified_name=match.group("name"),
                    kind=actual_kind,
                    signature=signature,
                    source_ids=[source_id],
                )
            )
    return symbols


def _node_export_symbols(metadata: dict, source_id: str) -> list[ContractSymbol]:
    exports = metadata.get("exports")
    if exports is None:
        return []
    if isinstance(exports, dict):
        values = sorted(
            (str(key), value)
            for key, value in exports.items()
            if str(key).startswith(".")
        )
    else:
        values = [(".", exports)]
    return [
        ContractSymbol(
            qualified_name=key,
            kind=SymbolKind.module_export,
            signature=json.dumps(value, sort_keys=True, separators=(",", ":")),
            source_ids=[source_id],
        )
        for key, value in values
    ]


def inspect_node_package(
    installed_root: Path,
    request: ContractRequest,
    identity: ContractCacheIdentity,
    *,
    check_runner: CheckRunner | None = None,
) -> tuple[ContractEvidence | None, list[ContractBlocker]]:
    """Inspect one installed npm package without evaluating JavaScript."""
    if request.exact_version is None:
        return None, [
            _blocker(
                request,
                BlockerCode.unresolved_version,
                "The repository profile did not resolve an exact package version.",
                request.manifest_path,
            )
        ]
    if not _NODE_NAME.fullmatch(request.package_name):
        return None, [
            _blocker(
                request,
                BlockerCode.inspection_failed,
                "The npm package name is invalid or unsafe.",
            )
        ]
    node_modules = (
        installed_root
        if installed_root.name == "node_modules"
        else installed_root / "node_modules"
    )
    package_root = node_modules.joinpath(*request.package_name.split("/"))
    try:
        package_root.resolve().relative_to(installed_root.resolve())
    except (OSError, ValueError):
        return None, [
            _blocker(
                request,
                BlockerCode.inspection_failed,
                "The installed package resolves outside the isolated installed tree.",
            )
        ]
    metadata_path = package_root / "package.json"
    raw, over_budget = _read_evidence(metadata_path)
    if raw is None:
        code = (
            BlockerCode.budget_exceeded
            if over_budget
            else BlockerCode.package_not_found
        )
        return None, [
            _blocker(
                request,
                code,
                "Installed package metadata is unavailable or exceeds the evidence budget.",
                metadata_path.as_posix(),
            )
        ]
    try:
        metadata = json.loads(raw)
    except ValueError:
        return None, [
            _blocker(
                request,
                BlockerCode.inspection_failed,
                "Installed package.json is malformed.",
                metadata_path.as_posix(),
            )
        ]
    if not isinstance(metadata, dict):
        return None, [
            _blocker(
                request,
                BlockerCode.inspection_failed,
                "Installed package.json is not an object.",
                metadata_path.as_posix(),
            )
        ]
    if (
        metadata.get("name") != request.package_name
        or str(metadata.get("version")) != request.exact_version
    ):
        return None, [
            _blocker(
                request,
                BlockerCode.version_mismatch,
                "Installed package identity does not match the locked request.",
                metadata_path.as_posix(),
            )
        ]

    provenance = _provenance(identity, "node_modules")
    metadata_source = _source(
        path=metadata_path,
        installed_root=installed_root,
        content=raw,
        kind=EvidenceSourceKind.installed_metadata,
        provenance=provenance,
    )
    sources = [metadata_source]
    symbols = _node_export_symbols(metadata, metadata_source.source_id)
    blockers: list[ContractBlocker] = []
    for path in _node_type_paths(package_root, metadata):
        content, over_budget = _read_evidence(path)
        if content is None:
            if over_budget:
                blockers.append(
                    _blocker(
                        request,
                        BlockerCode.budget_exceeded,
                        "An installed type declaration exceeds the evidence budget.",
                        path.as_posix(),
                        severity="warning",
                    )
                )
            continue
        source = _source(
            path=path,
            installed_root=installed_root,
            content=content,
            kind=EvidenceSourceKind.installed_types,
            provenance=provenance,
        )
        sources.append(source)
        symbols.extend(
            _node_symbols(content.decode("utf-8", "replace"), source.source_id)
        )

    if request.symbols:
        requested = set(request.symbols)
        requested_roots = {value.split(".", 1)[0] for value in requested}
        symbols = [
            item
            for item in symbols
            if item.qualified_name in requested
            or item.qualified_name in requested_roots
        ]
        found = {item.qualified_name for item in symbols}
        missing = sorted(requested - found)
        if missing:
            blockers.append(
                _blocker(
                    request,
                    BlockerCode.symbol_not_found,
                    "Requested symbols are absent from the installed declarations: "
                    + ", ".join(missing),
                    severity="warning",
                )
            )
    lifecycle = [
        LifecycleFact(
            kind=LifecycleKind.asynchronous,
            statement=f"{item.qualified_name} has an asynchronous installed type signature.",
            source_ids=item.source_ids,
        )
        for item in symbols
        if item.kind is SymbolKind.async_function
        or re.search(r"\bPromise\s*<", item.signature)
    ]
    examples: list[CompileCheckedExample] = []
    identifiers = sorted(
        item.qualified_name
        for item in symbols
        if re.fullmatch(r"[A-Za-z_$][\w$]*", item.qualified_name)
        and item.kind is not SymbolKind.module_export
    )
    if identifiers:
        snippet = (
            f'import {{ {", ".join(identifiers)} }} from "{request.package_name}";\n'
            + "\n".join(f"void {name};" for name in identifiers)
        )
        source_ids = sorted(
            {
                source_id
                for item in symbols
                if item.qualified_name in identifiers
                for source_id in item.source_ids
            }
        )
        example, blocker = _example(
            request=request,
            installed_root=installed_root,
            language="TypeScript",
            snippet=snippet,
            source_ids=source_ids,
            check_runner=check_runner,
        )
        if example is not None:
            examples.append(example)
        if blocker is not None:
            blockers.append(blocker)
    return _finish_evidence(request, sources, symbols, lifecycle, examples), blockers


def _normalized_python_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _python_metadata(
    installed_root: Path, package_name: str
) -> tuple[Path, bytes, str] | None:
    expected = _normalized_python_name(package_name)
    for metadata_path in sorted(installed_root.rglob("*.dist-info/METADATA")):
        try:
            metadata_path.resolve().relative_to(installed_root.resolve())
        except (OSError, ValueError):
            continue
        raw, over_budget = _read_evidence(metadata_path)
        if raw is None or over_budget:
            continue
        parsed = Parser().parsestr(raw.decode("utf-8", "replace"))
        if _normalized_python_name(parsed.get("Name", "")) == expected:
            return metadata_path, raw, parsed.get("Version", "")
    return None


def _python_import_roots(metadata_path: Path, package_name: str) -> list[str]:
    top_level = metadata_path.parent / "top_level.txt"
    try:
        values = [
            line.strip()
            for line in top_level.read_text(encoding="utf-8").splitlines()
            if re.fullmatch(r"[A-Za-z_]\w*", line.strip())
        ]
    except OSError:
        values = []
    fallback = package_name.replace("-", "_").replace(".", "_")
    return sorted(set(values or [fallback]))


def _python_module_files(
    site_packages: Path, import_roots: Iterable[str]
) -> list[Path]:
    candidates: list[Path] = []
    for name in import_roots:
        for relative in (f"{name}.pyi", f"{name}/__init__.pyi"):
            path = site_packages / relative
            if path.is_file():
                candidates.append(path)
        if not candidates:
            for relative in (f"{name}.py", f"{name}/__init__.py"):
                path = site_packages / relative
                if path.is_file():
                    candidates.append(path)
    return sorted(set(candidates))[:_MAX_TYPE_FILES]


def _python_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix} {node.name}({ast.unparse(node.args)}){returns}"


def _python_symbols(tree: ast.Module, source_id: str) -> list[ContractSymbol]:
    symbols: list[ContractSymbol] = []
    for node in tree.body:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and not node.name.startswith("_"):
            symbols.append(
                ContractSymbol(
                    qualified_name=node.name,
                    kind=(
                        SymbolKind.async_function
                        if isinstance(node, ast.AsyncFunctionDef)
                        else SymbolKind.function
                    ),
                    signature=_python_signature(node),
                    source_ids=[source_id],
                )
            )
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            bases = ", ".join(ast.unparse(base) for base in node.bases)
            suffix = f"({bases})" if bases else ""
            symbols.append(
                ContractSymbol(
                    qualified_name=node.name,
                    kind=SymbolKind.class_,
                    signature=f"class {node.name}{suffix}",
                    source_ids=[source_id],
                )
            )
            for member in node.body:
                if isinstance(
                    member, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and not member.name.startswith("_"):
                    symbols.append(
                        ContractSymbol(
                            qualified_name=f"{node.name}.{member.name}",
                            kind=SymbolKind.method,
                            signature=_python_signature(member),
                            source_ids=[source_id],
                        )
                    )
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and not node.target.id.startswith("_")
        ):
            symbols.append(
                ContractSymbol(
                    qualified_name=node.target.id,
                    kind=SymbolKind.constant,
                    signature=f"{node.target.id}: {ast.unparse(node.annotation)}",
                    source_ids=[source_id],
                )
            )
    return symbols


def inspect_python_package(
    installed_root: Path,
    request: ContractRequest,
    identity: ContractCacheIdentity,
    *,
    check_runner: CheckRunner | None = None,
) -> tuple[ContractEvidence | None, list[ContractBlocker]]:
    """Inspect one installed Python distribution without importing it."""
    if request.exact_version is None:
        return None, [
            _blocker(
                request,
                BlockerCode.unresolved_version,
                "The repository profile did not resolve an exact distribution version.",
                request.manifest_path,
            )
        ]
    metadata_record = _python_metadata(installed_root, request.package_name)
    if metadata_record is None:
        return None, [
            _blocker(
                request,
                BlockerCode.package_not_found,
                "Installed distribution metadata was not found.",
                installed_root.as_posix(),
            )
        ]
    metadata_path, metadata_raw, installed_version = metadata_record
    if installed_version != request.exact_version:
        return None, [
            _blocker(
                request,
                BlockerCode.version_mismatch,
                "Installed distribution version does not match the locked request.",
                metadata_path.as_posix(),
            )
        ]
    site_packages = metadata_path.parent.parent
    provenance = _provenance(identity, "site-packages")
    metadata_source = _source(
        path=metadata_path,
        installed_root=installed_root,
        content=metadata_raw,
        kind=EvidenceSourceKind.installed_metadata,
        provenance=provenance,
    )
    sources = [metadata_source]
    symbols: list[ContractSymbol] = []
    blockers: list[ContractBlocker] = []
    import_roots = _python_import_roots(metadata_path, request.package_name)
    for path in _python_module_files(site_packages, import_roots):
        try:
            path.resolve().relative_to(installed_root.resolve())
        except (OSError, ValueError):
            blockers.append(
                _blocker(
                    request,
                    BlockerCode.inspection_failed,
                    "An installed Python API file resolves outside the installed tree.",
                    path.as_posix(),
                    severity="warning",
                )
            )
            continue
        content, over_budget = _read_evidence(path)
        if content is None:
            if over_budget:
                blockers.append(
                    _blocker(
                        request,
                        BlockerCode.budget_exceeded,
                        "An installed Python API file exceeds the evidence budget.",
                        path.as_posix(),
                        severity="warning",
                    )
                )
            continue
        try:
            tree = ast.parse(content.decode("utf-8", "replace"), filename=path.name)
        except SyntaxError:
            blockers.append(
                _blocker(
                    request,
                    BlockerCode.inspection_failed,
                    "An installed Python API file could not be parsed statically.",
                    path.as_posix(),
                    severity="warning",
                )
            )
            continue
        kind = (
            EvidenceSourceKind.installed_types
            if path.suffix == ".pyi"
            else EvidenceSourceKind.installed_implementation
        )
        source = _source(
            path=path,
            installed_root=installed_root,
            content=content,
            kind=kind,
            provenance=provenance,
        )
        sources.append(source)
        symbols.extend(_python_symbols(tree, source.source_id))

    if request.symbols:
        requested = set(request.symbols)
        requested_roots = {value.split(".", 1)[0] for value in requested}
        symbols = [
            item
            for item in symbols
            if item.qualified_name in requested
            or item.qualified_name in requested_roots
        ]
        found = {item.qualified_name for item in symbols}
        missing = sorted(value for value in requested if value not in found)
        if missing:
            blockers.append(
                _blocker(
                    request,
                    BlockerCode.symbol_not_found,
                    "Requested symbols are absent from the installed Python API: "
                    + ", ".join(missing),
                    severity="warning",
                )
            )
    lifecycle = [
        LifecycleFact(
            kind=LifecycleKind.asynchronous,
            statement=f"{item.qualified_name} is declared with async def.",
            source_ids=item.source_ids,
        )
        for item in symbols
        if item.kind is SymbolKind.async_function
        or item.kind is SymbolKind.method
        and item.signature.startswith("async def")
    ]
    examples: list[CompileCheckedExample] = []
    top_level = sorted(
        item.qualified_name
        for item in symbols
        if "." not in item.qualified_name
        and re.fullmatch(r"[A-Za-z_]\w*", item.qualified_name)
    )
    if top_level and import_roots:
        snippet = f"from {import_roots[0]} import {', '.join(top_level)}\n" + "\n".join(
            f"_ = {name}" for name in top_level
        )
        source_ids = sorted(
            {
                source_id
                for item in symbols
                if item.qualified_name in top_level
                for source_id in item.source_ids
            }
        )
        example, blocker = _example(
            request=request,
            installed_root=installed_root,
            language="Python",
            snippet=snippet,
            source_ids=source_ids,
            check_runner=check_runner,
        )
        if example is not None:
            examples.append(example)
        if blocker is not None:
            blockers.append(blocker)
    if len(sources) == 1:
        blockers.append(
            _blocker(
                request,
                BlockerCode.inspection_failed,
                "No installed Python API file was available for static inspection.",
                severity="warning",
            )
        )
    return _finish_evidence(request, sources, symbols, lifecycle, examples), blockers
