"""Conservative local-import, route, link, and test discovery.

The tracers intentionally prefer an unresolved reference over a guessed edge.
They cover common forms in the repository profiler's initial ecosystems without
requiring language servers or executing repository code.
"""

from __future__ import annotations

import ast
import posixpath
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.inspection.repository import TextCollection

_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")
_SOURCE_EXTENSIONS = frozenset(
    {
        ".cs",
        ".go",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".mjs",
        ".py",
        ".rs",
        ".svelte",
        ".ts",
        ".tsx",
        ".vue",
    }
)

_LOCKFILE_NAMES = frozenset(
    {
        "Cargo.lock",
        "Gemfile.lock",
        "Pipfile.lock",
        "bun.lock",
        "bun.lockb",
        "composer.lock",
        "go.sum",
        "gradle.lockfile",
        "package-lock.json",
        "packages.lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
    }
)


@dataclass(frozen=True, order=True)
class ResolvedImport:
    source_path: str
    source_line: int
    target_path: str
    symbol: str | None = None


@dataclass(frozen=True, order=True)
class RouteFact:
    path: str
    line: int
    route: str


@dataclass(frozen=True, order=True)
class LinkFact:
    path: str
    line: int
    destination: str


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _join_relative(source_path: str, specifier: str) -> str | None:
    candidate = posixpath.normpath(
        posixpath.join(posixpath.dirname(source_path), specifier)
    )
    if candidate == ".." or candidate.startswith("../") or candidate.startswith("/"):
        return None
    return candidate.removeprefix("./")


def _resolve_file_base(
    base: str, paths: set[str], extensions: tuple[str, ...]
) -> str | None:
    candidates = [base]
    if not PurePosixPath(base).suffix:
        candidates.extend(base + extension for extension in extensions)
        candidates.extend(
            posixpath.join(base, "index" + extension) for extension in extensions
        )
    return next((candidate for candidate in candidates if candidate in paths), None)


def _javascript_imports(
    source_path: str, text: str, paths: set[str]
) -> list[ResolvedImport]:
    imports: list[ResolvedImport] = []
    patterns = (
        re.compile(r"\bfrom\s*['\"]([^'\"]+)['\"]"),
        re.compile(r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        re.compile(r"\bimport\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        re.compile(r"^\s*import\s*['\"]([^'\"]+)['\"]", re.MULTILINE),
    )
    seen: set[tuple[int, str]] = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            specifier = match.group(1)
            if not specifier.startswith(("./", "../")):
                continue
            base = _join_relative(source_path, specifier)
            target = (
                _resolve_file_base(base, paths, _JS_EXTENSIONS)
                if base is not None
                else None
            )
            if target is None:
                continue
            line = _line_number(text, match.start())
            key = (line, target)
            if key not in seen:
                seen.add(key)
                imports.append(ResolvedImport(source_path, line, target, specifier))
    return imports


def _python_module_target(module: str, paths: set[str]) -> str | None:
    base = module.replace(".", "/")
    return _resolve_file_base(base, paths, (".py",))


def _python_imports(
    source_path: str, text: str, paths: set[str]
) -> list[ResolvedImport]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    results: list[ResolvedImport] = []
    parent_parts = list(PurePosixPath(source_path).parent.parts)
    for node in ast.walk(tree):
        module: str | None = None
        symbols: str | None = None
        if isinstance(node, ast.ImportFrom):
            if node.level:
                keep = max(0, len(parent_parts) - (node.level - 1))
                parts = parent_parts[:keep]
                if node.module:
                    parts.extend(node.module.split("."))
                module = ".".join(parts)
            else:
                module = node.module
            symbols = ", ".join(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            # Each imported module is resolved independently below.
            for alias in node.names:
                target = _python_module_target(alias.name, paths)
                if target is not None:
                    results.append(
                        ResolvedImport(source_path, node.lineno, target, alias.name)
                    )
            continue
        if module:
            target = _python_module_target(module, paths)
            if target is not None:
                results.append(
                    ResolvedImport(source_path, node.lineno, target, symbols)
                )
    return results


def _go_module(collection: TextCollection) -> str | None:
    for path, inspected in collection.files.items():
        if PurePosixPath(path).name != "go.mod":
            continue
        match = re.search(r"(?m)^module\s+(\S+)\s*$", inspected.text)
        if match:
            return match.group(1).rstrip("/")
    return None


def _go_imports(
    source_path: str, text: str, paths: set[str], module: str | None
) -> list[ResolvedImport]:
    if module is None:
        return []
    results: list[ResolvedImport] = []
    expression = re.compile(r"\bimport\s*(?:\((.*?)\)|\"([^\"]+)\")", re.DOTALL)
    for match in expression.finditer(text):
        body = match.group(1)
        specs: list[tuple[str, int]] = []
        if body is None:
            specs.append((match.group(2), match.start(2)))
        else:
            for quoted in re.finditer(r'"([^\"]+)"', body):
                specs.append((quoted.group(1), match.start(1) + quoted.start(1)))
        for specifier, offset in specs:
            if specifier == module:
                base = "."
            elif specifier.startswith(module + "/"):
                base = specifier[len(module) + 1 :]
            else:
                continue
            prefix = "" if base == "." else base.rstrip("/") + "/"
            candidates = sorted(
                path
                for path in paths
                if path.startswith(prefix) and path.endswith(".go")
            )
            if candidates:
                results.append(
                    ResolvedImport(
                        source_path,
                        _line_number(text, offset),
                        candidates[0],
                        specifier,
                    )
                )
    return results


def _rust_crate_source_root(source_path: str, paths: set[str]) -> str:
    parts = list(PurePosixPath(source_path).parts)
    for index in range(len(parts) - 1, -1, -1):
        root = "/".join(parts[:index])
        manifest = f"{root + '/' if root else ''}Cargo.toml"
        if manifest in paths:
            return f"{root + '/' if root else ''}src"
    return "src"


def _resolve_rust_segments(
    base: str, segments: list[str], paths: set[str]
) -> str | None:
    cleaned = [
        segment
        for segment in segments
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", segment)
    ]
    for length in range(len(cleaned), 0, -1):
        candidate = posixpath.join(base, *cleaned[:length])
        target = _resolve_file_base(candidate, paths, (".rs",))
        if target is not None:
            return target
    return None


def _rust_imports(source_path: str, text: str, paths: set[str]) -> list[ResolvedImport]:
    results: list[ResolvedImport] = []
    crate_root = _rust_crate_source_root(source_path, paths)
    for match in re.finditer(r"(?m)^\s*use\s+(crate|self|super)(?:::(.*?))?;", text):
        anchor, tail = match.group(1), match.group(2) or ""
        segments = re.split(r"::", tail.replace("{", "::").replace("}", ""))
        if anchor == "crate":
            base = crate_root
        elif anchor == "self":
            base = posixpath.dirname(source_path)
        else:
            base = posixpath.dirname(posixpath.dirname(source_path))
        target = _resolve_rust_segments(base, segments, paths)
        if target is not None:
            results.append(
                ResolvedImport(
                    source_path,
                    _line_number(text, match.start()),
                    target,
                    tail,
                )
            )
    for match in re.finditer(r"(?m)^\s*mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;", text):
        target = _resolve_file_base(
            posixpath.join(posixpath.dirname(source_path), match.group(1)),
            paths,
            (".rs",),
        )
        if target is not None:
            results.append(
                ResolvedImport(
                    source_path,
                    _line_number(text, match.start()),
                    target,
                    match.group(1),
                )
            )
    return results


def _jvm_imports(source_path: str, text: str, paths: set[str]) -> list[ResolvedImport]:
    results: list[ResolvedImport] = []
    for match in re.finditer(r"(?m)^\s*import\s+([A-Za-z_][\w.]*)\s*;?", text):
        dotted = match.group(1)
        suffixes = (
            dotted.replace(".", "/") + ".java",
            dotted.replace(".", "/") + ".kt",
        )
        target = next(
            (
                path
                for path in sorted(paths)
                if any(path.endswith(suffix) for suffix in suffixes)
            ),
            None,
        )
        if target is not None:
            results.append(
                ResolvedImport(
                    source_path,
                    _line_number(text, match.start()),
                    target,
                    dotted,
                )
            )
    return results


def _csharp_namespace_index(collection: TextCollection) -> dict[str, list[str]]:
    namespaces: dict[str, list[str]] = {}
    for path, inspected in collection.files.items():
        if not path.endswith(".cs"):
            continue
        for match in re.finditer(r"\bnamespace\s+([A-Za-z_][\w.]*)", inspected.text):
            namespaces.setdefault(match.group(1), []).append(path)
    return namespaces


def _csharp_imports(
    source_path: str, text: str, namespaces: dict[str, list[str]]
) -> list[ResolvedImport]:
    results: list[ResolvedImport] = []
    for match in re.finditer(r"(?m)^\s*using\s+([A-Za-z_][\w.]*)\s*;", text):
        namespace = match.group(1)
        candidates = sorted(
            path
            for declared, paths in namespaces.items()
            if declared == namespace or declared.startswith(namespace + ".")
            for path in paths
            if path != source_path
        )
        if candidates:
            results.append(
                ResolvedImport(
                    source_path,
                    _line_number(text, match.start()),
                    candidates[0],
                    namespace,
                )
            )
    return results


def trace_local_imports(collection: TextCollection) -> list[ResolvedImport]:
    """Resolve only import forms that map unambiguously to repository files."""
    paths = set(collection.files)
    go_module = _go_module(collection)
    csharp_namespaces = _csharp_namespace_index(collection)
    imports: list[ResolvedImport] = []
    for path, inspected in collection.files.items():
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in _JS_EXTENSIONS:
            imports.extend(_javascript_imports(path, inspected.text, paths))
        elif suffix == ".py":
            imports.extend(_python_imports(path, inspected.text, paths))
        elif suffix == ".go":
            imports.extend(_go_imports(path, inspected.text, paths, go_module))
        elif suffix == ".rs":
            imports.extend(_rust_imports(path, inspected.text, paths))
        elif suffix in {".java", ".kt", ".kts"}:
            imports.extend(_jvm_imports(path, inspected.text, paths))
        elif suffix == ".cs":
            imports.extend(_csharp_imports(path, inspected.text, csharp_namespaces))
    return sorted(set(imports))


def _normalize_route(route: str) -> str:
    route = route.split("?", 1)[0].split("#", 1)[0].strip()
    if not route.startswith("/"):
        route = "/" + route
    route = re.sub(r"//+", "/", route)
    return route.rstrip("/") or "/"


def _filesystem_route(path: str) -> str | None:
    parts = list(PurePosixPath(path).parts)
    filename = PurePosixPath(path).name
    if filename.startswith("page.") and "app" in parts:
        index = len(parts) - 1 - list(reversed(parts)).index("app")
        route_parts = parts[index + 1 : -1]
    elif filename.startswith("route.") and "app" in parts:
        index = len(parts) - 1 - list(reversed(parts)).index("app")
        route_parts = parts[index + 1 : -1]
    elif "pages" in parts and PurePosixPath(path).suffix in _JS_EXTENSIONS:
        index = len(parts) - 1 - list(reversed(parts)).index("pages")
        route_parts = [*parts[index + 1 : -1], PurePosixPath(filename).stem]
        if route_parts and route_parts[-1] == "index":
            route_parts.pop()
    else:
        return None
    normalized: list[str] = []
    for part in route_parts:
        if part.startswith("(") and part.endswith(")"):
            continue
        if part.startswith("[[...") or part.startswith("[..."):
            normalized.append("*")
        elif part.startswith("[") and part.endswith("]"):
            normalized.append(":param")
        else:
            normalized.append(part)
    return _normalize_route("/" + "/".join(normalized))


_ROUTE_PATTERNS = (
    re.compile(
        r"@(?:app|router|blueprint)\.(?:get|post|put|patch|delete|route)\(\s*['\"]([^'\"]+)['\"]"
    ),
    re.compile(
        r"\b(?:app|router)\.(?:get|post|put|patch|delete|use)\(\s*['\"]([^'\"]+)['\"]"
    ),
    re.compile(r"\b(?:path|re_path)\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(
        r"@(?:Get|Post|Put|Patch|Delete|Request)Mapping\(\s*(?:value\s*=\s*)?['\"]([^'\"]+)['\"]"
    ),
    re.compile(
        r"\[(?:HttpGet|HttpPost|HttpPut|HttpPatch|HttpDelete|Route)\(\s*['\"]([^'\"]+)['\"]\s*\)\]"
    ),
    re.compile(r"\b(?:Handle|HandleFunc)\(\s*['\"]([^'\"]+)['\"]"),
)

_LINK_PATTERNS = (
    re.compile(r"\b(?:href|to)\s*=\s*['\"](/[^'\"]*)['\"]"),
    re.compile(r"\b(?:push|replace|redirect|fetch)\(\s*['\"](/[^'\"]*)['\"]"),
)


def discover_routes(collection: TextCollection) -> list[RouteFact]:
    routes: set[RouteFact] = set()
    for path, inspected in collection.files.items():
        filesystem_route = _filesystem_route(path)
        if filesystem_route is not None:
            routes.add(RouteFact(path, 1, filesystem_route))
        if PurePosixPath(path).suffix.lower() not in _SOURCE_EXTENSIONS:
            continue
        for pattern in _ROUTE_PATTERNS:
            for match in pattern.finditer(inspected.text):
                routes.add(
                    RouteFact(
                        path,
                        _line_number(inspected.text, match.start()),
                        _normalize_route(match.group(1)),
                    )
                )
    return sorted(routes)


def discover_links(collection: TextCollection) -> list[LinkFact]:
    links: set[LinkFact] = set()
    for path, inspected in collection.files.items():
        if PurePosixPath(path).suffix.lower() not in _SOURCE_EXTENSIONS:
            continue
        for pattern in _LINK_PATTERNS:
            for match in pattern.finditer(inspected.text):
                destination = match.group(1)
                if "${" in destination or PurePosixPath(destination).suffix:
                    continue
                links.add(
                    LinkFact(
                        path,
                        _line_number(inspected.text, match.start()),
                        _normalize_route(destination),
                    )
                )
    return sorted(links)


def route_matches(route: str, destination: str) -> bool:
    route_parts = [part for part in _normalize_route(route).split("/") if part]
    destination_parts = [
        part for part in _normalize_route(destination).split("/") if part
    ]
    for index, part in enumerate(route_parts):
        if part == "*":
            return True
        if index >= len(destination_parts):
            return False
        if part in {":param", "{id}"} or (part.startswith("{") and part.endswith("}")):
            continue
        if part != destination_parts[index]:
            return False
    return len(route_parts) == len(destination_parts)


def is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name.lower()
    parts = {part.lower() for part in pure.parts}
    return (
        "tests" in parts
        or "test" in parts
        or "__tests__" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("_test.go")
        or ".test." in name
        or ".spec." in name
        or name.endswith("test.java")
        or name.endswith("tests.cs")
    )


def _logical_stem(path: str) -> str:
    name = PurePosixPath(path).name.lower()
    for token in (".test.", ".spec."):
        if token in name:
            return name.split(token, 1)[0]
    stem = PurePosixPath(name).stem
    if stem.startswith("test_"):
        stem = stem[5:]
    for suffix in ("_test", "tests", "test"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem


def affected_test_paths(
    collection: TextCollection,
    changed_paths: set[str],
    imports: list[ResolvedImport],
) -> list[str]:
    """Find tests importing or conventionally paired with changed source files."""
    changed_stems = {_logical_stem(path) for path in changed_paths}
    imported = {
        relation.source_path
        for relation in imports
        if relation.target_path in changed_paths and is_test_path(relation.source_path)
    }
    conventional = {
        path
        for path in collection.files
        if is_test_path(path) and _logical_stem(path) in changed_stems
    }
    changed_tests = {path for path in changed_paths if is_test_path(path)}
    return sorted(imported | conventional | changed_tests)


def lockfile_paths(collection: TextCollection) -> list[str]:
    return sorted(
        path for path in collection.files if PurePosixPath(path).name in _LOCKFILE_NAMES
    )
