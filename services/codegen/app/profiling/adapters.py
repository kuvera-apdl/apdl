"""Initial ecosystem adapters for the canonical repository profiler."""

from __future__ import annotations

import json
import re
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

from app.inspection.repository import RepositoryTextView
from app.profiling.models import (
    CodeSurface,
    CommandKind,
    Dependency,
    PackageBoundary,
    PackageManager,
    RepoCommand,
    TestFacility,
    Uncertainty,
    UncertaintyCode,
    WorkspaceBoundary,
)


@dataclass
class ProfileFragment:
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    package_managers: list[PackageManager] = field(default_factory=list)
    lockfiles: list[str] = field(default_factory=list)
    workspaces: list[WorkspaceBoundary] = field(default_factory=list)
    packages: list[PackageBoundary] = field(default_factory=list)
    commands: list[RepoCommand] = field(default_factory=list)
    test_facilities: list[TestFacility] = field(default_factory=list)
    routes: list[CodeSurface] = field(default_factory=list)
    entrypoints: list[CodeSurface] = field(default_factory=list)
    services: list[CodeSurface] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    uncertainties: list[Uncertainty] = field(default_factory=list)


class EcosystemAdapter(Protocol):
    name: str

    def detect(self, paths: list[str]) -> bool: ...

    def profile(
        self,
        root: Path,
        paths: list[str],
        contents: RepositoryTextView,
    ) -> ProfileFragment: ...


def _rel(path: Path, root: Path) -> str:
    value = path.relative_to(root).as_posix()
    return value or "."


def _cwd(manifest: Path, root: Path) -> str:
    value = manifest.parent.relative_to(root).as_posix()
    return value or "."


def _json(path: Path, root: Path, contents: RepositoryTextView) -> dict | None:
    text = contents.text(_rel(path, root))
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _toml(path: Path, root: Path, contents: RepositoryTextView) -> dict | None:
    text = contents.text(_rel(path, root))
    if text is None:
        return None
    try:
        value = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _text(path: Path, root: Path, contents: RepositoryTextView) -> str:
    return contents.text(_rel(path, root)) or ""


def _exists(path: Path, root: Path, contents: RepositoryTextView) -> bool:
    return contents.contains(_rel(path, root))


def _uncertainty(code: UncertaintyCode, message: str, *paths: str) -> Uncertainty:
    return Uncertainty(code=code, message=message, paths=sorted(set(paths)))


def _command(
    kind: CommandKind, command: str, cwd: str, source_path: str
) -> RepoCommand:
    return RepoCommand(kind=kind, command=command, cwd=cwd, source_path=source_path)


def _workspace_members(
    root: Path,
    base: Path,
    patterns: list[str],
    paths: list[str],
) -> list[str]:
    members: set[str] = set()
    base_path = PurePosixPath(_cwd(base / "package.json", root))
    for pattern in patterns:
        manifest_pattern = f"{pattern.rstrip('/')}/package.json"
        for path in paths:
            candidate = PurePosixPath(path)
            try:
                relative = candidate.relative_to(base_path)
            except ValueError:
                continue
            if relative.match(manifest_pattern):
                members.add(candidate.parent.as_posix() or ".")
    return sorted(members)


def _nearest_lockfiles(
    manifest_cwd: str, paths: list[str], lock_names: set[str]
) -> list[str]:
    manifest_dir = Path() if manifest_cwd == "." else Path(manifest_cwd)
    candidates: list[tuple[int, str]] = []
    for path in paths:
        if Path(path).name not in lock_names:
            continue
        lock_dir = Path(path).parent
        try:
            manifest_dir.relative_to(lock_dir)
        except ValueError:
            continue
        candidates.append((len(lock_dir.parts), path))
    if not candidates:
        return []
    deepest = max(depth for depth, _ in candidates)
    return sorted(path for depth, path in candidates if depth == deepest)


class NodeAdapter:
    name = "node"
    _lock_managers = {
        "package-lock.json": "npm",
        "npm-shrinkwrap.json": "npm",
        "pnpm-lock.yaml": "pnpm",
        "yarn.lock": "yarn",
        "bun.lock": "bun",
        "bun.lockb": "bun",
    }

    def detect(self, paths: list[str]) -> bool:
        return any(path.endswith("package.json") for path in paths)

    def profile(
        self,
        root: Path,
        paths: list[str],
        contents: RepositoryTextView,
    ) -> ProfileFragment:
        out = ProfileFragment(languages=["JavaScript"])
        if any(path.endswith((".ts", ".tsx", "tsconfig.json")) for path in paths):
            out.languages.append("TypeScript")
        manifests = [root / path for path in paths if Path(path).name == "package.json"]
        all_dependencies: set[str] = set()
        resolved: dict[str, tuple[str, str]] = {}
        for lock in (
            root / path for path in paths if Path(path).name == "package-lock.json"
        ):
            data = _json(lock, root, contents) or {}
            for key, record in (data.get("packages") or {}).items():
                if not key or not isinstance(record, dict) or not record.get("version"):
                    continue
                name = key.rsplit("node_modules/", 1)[-1]
                resolved[name] = (str(record["version"]), _rel(lock, root))

        for manifest in manifests:
            rel = _rel(manifest, root)
            cwd = _cwd(manifest, root)
            data = _json(manifest, root, contents)
            if data is None:
                out.uncertainties.append(
                    _uncertainty(
                        UncertaintyCode.malformed_manifest,
                        "Malformed package.json.",
                        rel,
                    )
                )
                continue
            out.packages.append(
                PackageBoundary(
                    path=cwd,
                    ecosystem="node",
                    name=str(data.get("name")) if data.get("name") else None,
                    manifest_path=rel,
                )
            )
            deps: dict[str, str] = {}
            for section in (
                "dependencies",
                "devDependencies",
                "peerDependencies",
                "optionalDependencies",
            ):
                values = data.get(section)
                if isinstance(values, dict):
                    deps.update({str(k): str(v) for k, v in values.items()})
            all_dependencies.update(deps)
            for name, constraint in deps.items():
                version_source = resolved.get(name)
                out.dependencies.append(
                    Dependency(
                        name=name,
                        ecosystem="node",
                        package_path=cwd,
                        declared_constraint=constraint,
                        resolved_version=version_source[0] if version_source else None,
                        source_path=version_source[1] if version_source else rel,
                    )
                )
            package_manager = data.get("packageManager")
            declared_name = declared_version = None
            if isinstance(package_manager, str) and "@" in package_manager:
                declared_name, declared_version = package_manager.split("@", 1)
            local_locks = _nearest_lockfiles(cwd, paths, set(self._lock_managers))
            managers = {self._lock_managers[Path(path).name] for path in local_locks}
            command_manager = (
                declared_name
                or (next(iter(managers)) if len(managers) == 1 else None)
                or "npm"
            )
            command_prefix = {
                "npm": "npm run",
                "pnpm": "pnpm",
                "yarn": "yarn",
                "bun": "bun run",
            }.get(command_manager, f"{command_manager} run")
            scripts = (
                data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
            )
            for name, value in scripts.items():
                lower = str(name).lower()
                kind = next(
                    (
                        kind
                        for marker, kind in (
                            ("format", CommandKind.format),
                            ("fmt", CommandKind.format),
                            ("lint", CommandKind.lint),
                            ("typecheck", CommandKind.typecheck),
                            ("type-check", CommandKind.typecheck),
                            ("build", CommandKind.build),
                            ("test", CommandKind.test),
                        )
                        if marker == lower or lower.startswith(marker + ":")
                    ),
                    None,
                )
                if kind:
                    out.commands.append(
                        _command(kind, f"{command_prefix} {name}", cwd, rel)
                    )
            workspaces = data.get("workspaces")
            patterns = (
                workspaces.get("packages", [])
                if isinstance(workspaces, dict)
                else workspaces
            )
            if isinstance(patterns, list):
                string_patterns = [str(item) for item in patterns]
                out.workspaces.append(
                    WorkspaceBoundary(
                        root=cwd,
                        ecosystem="node",
                        members=_workspace_members(
                            root,
                            manifest.parent,
                            string_patterns,
                            paths,
                        ),
                        source_path=rel,
                    )
                )
            for lock_path in sorted(local_locks):
                manager = self._lock_managers[Path(lock_path).name]
                out.package_managers.append(
                    PackageManager(
                        name=manager,
                        manifest_path=rel,
                        lockfile_path=lock_path,
                        declared_version=declared_version
                        if declared_name == manager
                        else None,
                    )
                )
                out.lockfiles.append(lock_path)
            if len(managers) > 1:
                out.uncertainties.append(
                    _uncertainty(
                        UncertaintyCode.conflicting_package_managers,
                        f"Conflicting Node package-manager lockfiles: {', '.join(sorted(managers))}.",
                        *local_locks,
                    )
                )
            elif not local_locks:
                name = declared_name or "unknown"
                out.package_managers.append(
                    PackageManager(
                        name=name, manifest_path=rel, declared_version=declared_version
                    )
                )
                if name == "unknown":
                    out.uncertainties.append(
                        _uncertainty(
                            UncertaintyCode.package_manager_unknown,
                            "No Node package-manager declaration or lockfile was found.",
                            rel,
                        )
                    )
            unresolved = sorted(name for name in deps if name not in resolved)
            if unresolved:
                out.uncertainties.append(
                    _uncertainty(
                        UncertaintyCode.unresolved_dependency_versions,
                        f"Exact resolved versions unavailable for {len(unresolved)} Node dependencies.",
                        rel,
                    )
                )

        framework_map = {
            "next": "Next.js",
            "react": "React",
            "vue": "Vue",
            "svelte": "Svelte",
            "@angular/core": "Angular",
            "express": "Express",
            "fastify": "Fastify",
            "nestjs": "NestJS",
            "@nestjs/core": "NestJS",
        }
        out.frameworks.extend(
            label for dep, label in framework_map.items() if dep in all_dependencies
        )
        for dep, browser in (
            ("vitest", False),
            ("jest", False),
            ("mocha", False),
            ("playwright", True),
            ("@playwright/test", True),
            ("cypress", True),
        ):
            if dep in all_dependencies:
                out.test_facilities.append(
                    TestFacility(
                        name=dep,
                        package_path=".",
                        browser=browser,
                        source_path="package.json",
                    )
                )
        for path in paths:
            if re.search(r"(^|/)app/(.+/)?(page|route)\.(js|jsx|ts|tsx)$", path):
                out.routes.append(CodeSurface(kind="next_app_route", path=path))
            elif re.search(r"(^|/)pages/(?!_)[^/]+\.(js|jsx|ts|tsx)$", path):
                out.routes.append(CodeSurface(kind="next_pages_route", path=path))
            if Path(path).name in {
                "server.js",
                "server.ts",
                "index.js",
                "index.ts",
            } and Path(path).parent.as_posix() in {".", "src"}:
                out.entrypoints.append(CodeSurface(kind="node_entrypoint", path=path))
        return out


class PythonAdapter:
    name = "python"

    def detect(self, paths: list[str]) -> bool:
        names = {Path(path).name for path in paths}
        return bool(
            names & {"pyproject.toml", "setup.py", "requirements.txt", "Pipfile"}
        )

    def profile(
        self,
        root: Path,
        paths: list[str],
        contents: RepositoryTextView,
    ) -> ProfileFragment:
        out = ProfileFragment(languages=["Python"])
        manifests = [
            root / path for path in paths if Path(path).name == "pyproject.toml"
        ]
        resolved: dict[str, tuple[str, str]] = {}
        for lock_name in ("uv.lock", "poetry.lock", "pdm.lock"):
            for lock in (root / path for path in paths if Path(path).name == lock_name):
                data = _toml(lock, root, contents) or {}
                for package in data.get("package", []):
                    if (
                        isinstance(package, dict)
                        and package.get("name")
                        and package.get("version")
                    ):
                        resolved[str(package["name"]).lower()] = (
                            str(package["version"]),
                            _rel(lock, root),
                        )
                out.lockfiles.append(_rel(lock, root))
        for req in (
            root / path for path in paths if Path(path).match("requirements*.txt")
        ):
            for line in _text(req, root, contents).splitlines():
                match = re.match(r"\s*([A-Za-z0-9_.-]+)==([^\s;]+)", line)
                if match:
                    resolved[match.group(1).lower()] = (match.group(2), _rel(req, root))
        for manifest in manifests:
            rel, cwd = _rel(manifest, root), _cwd(manifest, root)
            data = _toml(manifest, root, contents)
            if data is None:
                out.uncertainties.append(
                    _uncertainty(
                        UncertaintyCode.malformed_manifest,
                        "Malformed pyproject.toml.",
                        rel,
                    )
                )
                continue
            project = (
                data.get("project") if isinstance(data.get("project"), dict) else {}
            )
            name = str(project.get("name")) if project.get("name") else None
            out.packages.append(
                PackageBoundary(
                    path=cwd, ecosystem="python", name=name, manifest_path=rel
                )
            )
            raw_deps = (
                project.get("dependencies")
                if isinstance(project.get("dependencies"), list)
                else []
            )
            poetry = (
                ((data.get("tool") or {}).get("poetry") or {})
                if isinstance(data.get("tool"), dict)
                else {}
            )
            poetry_deps = (
                poetry.get("dependencies")
                if isinstance(poetry.get("dependencies"), dict)
                else {}
            )
            dependencies: dict[str, str] = {}
            for raw in raw_deps:
                match = re.match(r"\s*([A-Za-z0-9_.-]+)(.*)", str(raw))
                if match:
                    dependencies[match.group(1)] = match.group(2).strip() or "*"
            dependencies.update(
                {
                    str(k): str(v)
                    for k, v in poetry_deps.items()
                    if str(k).lower() != "python"
                }
            )
            for dep, constraint in dependencies.items():
                version = resolved.get(dep.lower())
                out.dependencies.append(
                    Dependency(
                        name=dep,
                        ecosystem="python",
                        package_path=cwd,
                        declared_constraint=constraint,
                        resolved_version=version[0] if version else None,
                        source_path=version[1] if version else rel,
                    )
                )
            if dependencies and any(
                dep.lower() not in resolved for dep in dependencies
            ):
                out.uncertainties.append(
                    _uncertainty(
                        UncertaintyCode.unresolved_dependency_versions,
                        "Some Python dependencies lack exact resolved versions.",
                        rel,
                    )
                )
            tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
            if "ruff" in tool or "ruff" in {dep.lower() for dep in dependencies}:
                out.commands.extend(
                    [
                        _command(CommandKind.format, "ruff format .", cwd, rel),
                        _command(CommandKind.lint, "ruff check .", cwd, rel),
                    ]
                )
            if "mypy" in tool or "mypy" in {dep.lower() for dep in dependencies}:
                out.commands.append(_command(CommandKind.typecheck, "mypy .", cwd, rel))
            if (
                "pytest" in tool
                or "pytest" in {dep.lower() for dep in dependencies}
                or any(Path(path).name == "pytest.ini" for path in paths)
            ):
                out.commands.append(
                    _command(CommandKind.test, "python -m pytest", cwd, rel)
                )
                out.test_facilities.append(
                    TestFacility(name="pytest", package_path=cwd, source_path=rel)
                )
            for dep, framework in (
                ("django", "Django"),
                ("fastapi", "FastAPI"),
                ("flask", "Flask"),
                ("starlette", "Starlette"),
            ):
                if dep in {name.lower() for name in dependencies}:
                    out.frameworks.append(framework)
        lock_managers = {
            Path(path).name: manager
            for path, manager in ((p, "uv") for p in paths if Path(p).name == "uv.lock")
        }
        lock_managers.update(
            {
                Path(path).name: "poetry"
                for path in paths
                if Path(path).name == "poetry.lock"
            }
        )
        lock_managers.update(
            {Path(path).name: "pdm" for path in paths if Path(path).name == "pdm.lock"}
        )
        if len(set(lock_managers.values())) > 1:
            out.uncertainties.append(
                _uncertainty(
                    UncertaintyCode.conflicting_package_managers,
                    "Conflicting Python lockfile managers detected.",
                    *[p for p in paths if Path(p).name in lock_managers],
                )
            )
        if manifests and not lock_managers:
            for manifest in manifests:
                out.package_managers.append(
                    PackageManager(name="unknown", manifest_path=_rel(manifest, root))
                )
            out.uncertainties.append(
                _uncertainty(
                    UncertaintyCode.package_manager_unknown,
                    "No Python lockfile identified the package manager.",
                    *[_rel(manifest, root) for manifest in manifests],
                )
            )
        for path in paths:
            name = Path(path).name
            if name in {"uv.lock", "poetry.lock", "pdm.lock"}:
                out.package_managers.append(
                    PackageManager(
                        name=lock_managers[name],
                        manifest_path=str(Path(path).parent / "pyproject.toml"),
                        lockfile_path=path,
                    )
                )
            if name in {"main.py", "app.py", "manage.py", "wsgi.py", "asgi.py"}:
                out.entrypoints.append(
                    CodeSurface(
                        kind="python_entrypoint",
                        path=path,
                        package_path=Path(path).parent.as_posix() or ".",
                    )
                )
            if name == "urls.py":
                out.routes.append(
                    CodeSurface(
                        kind="django_routes",
                        path=path,
                        package_path=Path(path).parent.as_posix() or ".",
                    )
                )
        return out


class GoAdapter:
    name = "go"

    def detect(self, paths: list[str]) -> bool:
        return any(Path(path).name in {"go.mod", "go.work"} for path in paths)

    def profile(
        self,
        root: Path,
        paths: list[str],
        contents: RepositoryTextView,
    ) -> ProfileFragment:
        out = ProfileFragment(languages=["Go"])
        for workspace in (
            root / path for path in paths if Path(path).name == "go.work"
        ):
            members = re.findall(
                r"(?m)^\s*(\./[^\s)]+)",
                _text(workspace, root, contents),
            )
            out.workspaces.append(
                WorkspaceBoundary(
                    root=_cwd(workspace, root),
                    ecosystem="go",
                    members=sorted(member.removeprefix("./") for member in members),
                    source_path=_rel(workspace, root),
                )
            )
        for manifest in (root / path for path in paths if Path(path).name == "go.mod"):
            rel, cwd, text = (
                _rel(manifest, root),
                _cwd(manifest, root),
                _text(manifest, root, contents),
            )
            module = re.search(r"(?m)^module\s+(\S+)", text)
            out.packages.append(
                PackageBoundary(
                    path=cwd,
                    ecosystem="go",
                    name=module.group(1) if module else None,
                    manifest_path=rel,
                )
            )
            for name, version in re.findall(
                r"(?m)^(?:require\s+|\s+)([A-Za-z0-9_.\-/]+)\s+(v[^\s]+)",
                text,
            ):
                out.dependencies.append(
                    Dependency(
                        name=name,
                        ecosystem="go",
                        package_path=cwd,
                        declared_constraint=version,
                        resolved_version=version,
                        source_path=rel,
                    )
                )
            out.package_managers.append(
                PackageManager(
                    name="go_modules",
                    manifest_path=rel,
                    lockfile_path=str(Path(cwd) / "go.sum")
                    if _exists(manifest.parent / "go.sum", root, contents)
                    else None,
                )
            )
            if _exists(manifest.parent / "go.sum", root, contents):
                out.lockfiles.append(_rel(manifest.parent / "go.sum", root))
            out.commands.extend(
                [
                    _command(CommandKind.format, "gofmt -w .", cwd, rel),
                    _command(CommandKind.lint, "go vet ./...", cwd, rel),
                    _command(CommandKind.build, "go build ./...", cwd, rel),
                    _command(CommandKind.test, "go test ./...", cwd, rel),
                ]
            )
            out.test_facilities.append(
                TestFacility(name="go test", package_path=cwd, source_path=rel)
            )
        for path in paths:
            if path.endswith(".go") and re.search(
                r"(?m)^package\s+main\b",
                _text(root / path, root, contents),
            ):
                out.entrypoints.append(
                    CodeSurface(
                        kind="go_main",
                        path=path,
                        package_path=Path(path).parent.as_posix() or ".",
                    )
                )
        return out


class RustAdapter:
    name = "rust"

    def detect(self, paths: list[str]) -> bool:
        return any(Path(path).name == "Cargo.toml" for path in paths)

    def profile(
        self,
        root: Path,
        paths: list[str],
        contents: RepositoryTextView,
    ) -> ProfileFragment:
        out = ProfileFragment(languages=["Rust"])
        resolved: dict[str, tuple[str, str]] = {}
        for lock in (root / path for path in paths if Path(path).name == "Cargo.lock"):
            data = _toml(lock, root, contents) or {}
            for package in data.get("package", []):
                if (
                    isinstance(package, dict)
                    and package.get("name")
                    and package.get("version")
                ):
                    resolved[str(package["name"])] = (
                        str(package["version"]),
                        _rel(lock, root),
                    )
            out.lockfiles.append(_rel(lock, root))
        for manifest in (
            root / path for path in paths if Path(path).name == "Cargo.toml"
        ):
            rel, cwd, data = (
                _rel(manifest, root),
                _cwd(manifest, root),
                _toml(manifest, root, contents),
            )
            if data is None:
                out.uncertainties.append(
                    _uncertainty(
                        UncertaintyCode.malformed_manifest, "Malformed Cargo.toml.", rel
                    )
                )
                continue
            package = (
                data.get("package") if isinstance(data.get("package"), dict) else {}
            )
            out.packages.append(
                PackageBoundary(
                    path=cwd,
                    ecosystem="rust",
                    name=str(package.get("name")) if package.get("name") else None,
                    manifest_path=rel,
                )
            )
            for section in ("dependencies", "dev-dependencies", "build-dependencies"):
                deps = data.get(section) if isinstance(data.get(section), dict) else {}
                for name, constraint in deps.items():
                    constraint_text = (
                        constraint
                        if isinstance(constraint, str)
                        else str((constraint or {}).get("version") or "*")
                    )
                    version = resolved.get(str(name))
                    out.dependencies.append(
                        Dependency(
                            name=str(name),
                            ecosystem="rust",
                            package_path=cwd,
                            declared_constraint=str(constraint_text),
                            resolved_version=version[0] if version else None,
                            source_path=version[1] if version else rel,
                        )
                    )
            members = (
                ((data.get("workspace") or {}).get("members") or [])
                if isinstance(data.get("workspace"), dict)
                else []
            )
            if members:
                out.workspaces.append(
                    WorkspaceBoundary(
                        root=cwd,
                        ecosystem="rust",
                        members=sorted(str(item) for item in members),
                        source_path=rel,
                    )
                )
            lock = manifest.parent / "Cargo.lock"
            out.package_managers.append(
                PackageManager(
                    name="cargo",
                    manifest_path=rel,
                    lockfile_path=(
                        _rel(lock, root) if _exists(lock, root, contents) else None
                    ),
                )
            )
            out.commands.extend(
                [
                    _command(CommandKind.format, "cargo fmt --check", cwd, rel),
                    _command(
                        CommandKind.lint,
                        "cargo clippy --all-targets --all-features -- -D warnings",
                        cwd,
                        rel,
                    ),
                    _command(CommandKind.build, "cargo build", cwd, rel),
                    _command(CommandKind.test, "cargo test", cwd, rel),
                ]
            )
            out.test_facilities.append(
                TestFacility(name="cargo test", package_path=cwd, source_path=rel)
            )
        for path in paths:
            if path.endswith("/src/main.rs") or path == "src/main.rs":
                out.entrypoints.append(
                    CodeSurface(
                        kind="rust_binary",
                        path=path,
                        package_path=Path(path).parent.parent.as_posix() or ".",
                    )
                )
        return out


class JVMAdapter:
    name = "jvm"

    def detect(self, paths: list[str]) -> bool:
        names = {Path(path).name for path in paths}
        return "pom.xml" in names or bool(
            names
            & {
                "build.gradle",
                "build.gradle.kts",
                "settings.gradle",
                "settings.gradle.kts",
            }
        )

    def profile(
        self,
        root: Path,
        paths: list[str],
        contents: RepositoryTextView,
    ) -> ProfileFragment:
        out = ProfileFragment(languages=["JVM"])
        for settings in (
            root / path
            for path in paths
            if Path(path).name in {"settings.gradle", "settings.gradle.kts"}
        ):
            members = [
                value.replace(":", "/").strip("/")
                for value in re.findall(
                    r"['\"](:[^'\"]+)['\"]",
                    _text(settings, root, contents),
                )
            ]
            out.workspaces.append(
                WorkspaceBoundary(
                    root=_cwd(settings, root),
                    ecosystem="jvm",
                    members=sorted(set(members)),
                    source_path=_rel(settings, root),
                )
            )
        for path in paths:
            name = Path(path).name
            cwd = Path(path).parent.as_posix() or "."
            if name in {"build.gradle", "build.gradle.kts"}:
                text = _text(root / path, root, contents)
                lock_path = root / cwd / "gradle.lockfile"
                locked: dict[str, str] = {}
                for line in _text(lock_path, root, contents).splitlines():
                    match = re.match(r"([^:]+:[^:]+):([^=]+)=", line.strip())
                    if match:
                        locked[match.group(1)] = match.group(2)
                manager = (
                    "gradle_wrapper"
                    if _exists(root / cwd / "gradlew", root, contents)
                    else "gradle"
                )
                prefix = "./gradlew" if manager == "gradle_wrapper" else "gradle"
                out.packages.append(
                    PackageBoundary(path=cwd, ecosystem="jvm", manifest_path=path)
                )
                out.package_managers.append(
                    PackageManager(
                        name=manager,
                        manifest_path=path,
                        lockfile_path=(
                            _rel(lock_path, root)
                            if _exists(lock_path, root, contents)
                            else None
                        ),
                    )
                )
                if _exists(lock_path, root, contents):
                    out.lockfiles.append(_rel(lock_path, root))
                out.commands.extend(
                    [
                        _command(CommandKind.build, f"{prefix} build", cwd, path),
                        _command(CommandKind.test, f"{prefix} test", cwd, path),
                    ]
                )
                out.test_facilities.append(
                    TestFacility(name="Gradle Test", package_path=cwd, source_path=path)
                )
                for group, artifact, version in re.findall(
                    r"['\"]([\w.-]+):([\w.-]+):([^'\"]+)['\"]", text
                ):
                    dependency_name = f"{group}:{artifact}"
                    out.dependencies.append(
                        Dependency(
                            name=dependency_name,
                            ecosystem="jvm",
                            package_path=cwd,
                            declared_constraint=version,
                            resolved_version=locked.get(dependency_name),
                            source_path=_rel(lock_path, root)
                            if dependency_name in locked
                            else path,
                        )
                    )
                if "org.springframework" in text:
                    out.frameworks.append("Spring")
            elif name == "pom.xml":
                out.packages.append(
                    PackageBoundary(path=cwd, ecosystem="jvm", manifest_path=path)
                )
                out.package_managers.append(
                    PackageManager(name="maven", manifest_path=path)
                )
                out.commands.extend(
                    [
                        _command(
                            CommandKind.build,
                            "./mvnw verify"
                            if _exists(root / cwd / "mvnw", root, contents)
                            else "mvn verify",
                            cwd,
                            path,
                        ),
                        _command(
                            CommandKind.test,
                            "./mvnw test"
                            if _exists(root / cwd / "mvnw", root, contents)
                            else "mvn test",
                            cwd,
                            path,
                        ),
                    ]
                )
                out.test_facilities.append(
                    TestFacility(
                        name="Maven Surefire", package_path=cwd, source_path=path
                    )
                )
                try:
                    tree = ET.ElementTree(
                        ET.fromstring(_text(root / path, root, contents))
                    )
                    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
                    for dep in tree.findall(".//m:dependency", ns):
                        group = dep.findtext("m:groupId", default="", namespaces=ns)
                        artifact = dep.findtext(
                            "m:artifactId", default="", namespaces=ns
                        )
                        version = dep.findtext("m:version", default="", namespaces=ns)
                        if group and artifact:
                            out.dependencies.append(
                                Dependency(
                                    name=f"{group}:{artifact}",
                                    ecosystem="jvm",
                                    package_path=cwd,
                                    declared_constraint=version or None,
                                    resolved_version=version or None,
                                    source_path=path,
                                )
                            )
                except ET.ParseError:
                    out.uncertainties.append(
                        _uncertainty(
                            UncertaintyCode.malformed_manifest,
                            "Malformed Maven pom.xml.",
                            path,
                        )
                    )
        if out.dependencies and any(
            dep.resolved_version is None for dep in out.dependencies
        ):
            out.uncertainties.append(
                _uncertainty(
                    UncertaintyCode.unresolved_dependency_versions,
                    "JVM dependency locks are incomplete or unavailable.",
                    *[p.manifest_path for p in out.packages],
                )
            )
        for path in paths:
            if path.endswith(("Application.java", "Application.kt")):
                out.entrypoints.append(
                    CodeSurface(kind="jvm_application", path=path, package_path=".")
                )
        return out


class DotNetAdapter:
    name = "dotnet"

    def detect(self, paths: list[str]) -> bool:
        return any(path.endswith((".sln", ".csproj", ".fsproj")) for path in paths)

    def profile(
        self,
        root: Path,
        paths: list[str],
        contents: RepositoryTextView,
    ) -> ProfileFragment:
        out = ProfileFragment(
            languages=["C#"]
            if any(path.endswith(".csproj") for path in paths)
            else ["F#"]
        )
        for solution in (root / path for path in paths if path.endswith(".sln")):
            members = [
                match.replace("\\", "/")
                for match in re.findall(
                    r'Project\([^)]*\)\s*=\s*"[^"]+",\s*"([^"]+\.(?:csproj|fsproj))"',
                    _text(solution, root, contents),
                )
            ]
            out.workspaces.append(
                WorkspaceBoundary(
                    root=_cwd(solution, root),
                    ecosystem="dotnet",
                    members=sorted(members),
                    source_path=_rel(solution, root),
                )
            )
        for path in paths:
            if not path.endswith((".csproj", ".fsproj")):
                continue
            cwd = Path(path).parent.as_posix() or "."
            out.packages.append(
                PackageBoundary(
                    path=cwd,
                    ecosystem="dotnet",
                    name=Path(path).stem,
                    manifest_path=path,
                )
            )
            lock = root / cwd / "packages.lock.json"
            locked: dict[str, str] = {}
            lock_data = (
                _json(lock, root, contents) if _exists(lock, root, contents) else None
            )
            for target in (lock_data or {}).get("dependencies", {}).values():
                if not isinstance(target, dict):
                    continue
                for dependency_name, record in target.items():
                    if isinstance(record, dict) and record.get("resolved"):
                        locked[str(dependency_name)] = str(record["resolved"])
            out.package_managers.append(
                PackageManager(
                    name="nuget",
                    manifest_path=path,
                    lockfile_path=(
                        _rel(lock, root) if _exists(lock, root, contents) else None
                    ),
                )
            )
            if _exists(lock, root, contents):
                out.lockfiles.append(_rel(lock, root))
            try:
                tree = ET.ElementTree(ET.fromstring(_text(root / path, root, contents)))
                for ref in tree.findall(".//PackageReference"):
                    name = ref.get("Include") or ref.get("Update")
                    version = ref.get("Version") or ref.findtext("Version")
                    if name:
                        out.dependencies.append(
                            Dependency(
                                name=name,
                                ecosystem="dotnet",
                                package_path=cwd,
                                declared_constraint=version,
                                resolved_version=locked.get(name),
                                source_path=_rel(lock, root)
                                if name in locked
                                else path,
                            )
                        )
            except ET.ParseError:
                out.uncertainties.append(
                    _uncertainty(
                        UncertaintyCode.malformed_manifest,
                        "Malformed .NET project file.",
                        path,
                    )
                )
            if "test" in Path(path).stem.lower():
                out.test_facilities.append(
                    TestFacility(name="dotnet test", package_path=cwd, source_path=path)
                )
        source = next(
            (path for path in paths if path.endswith(".sln")),
            out.packages[0].manifest_path if out.packages else ".",
        )
        out.commands.extend(
            [
                _command(
                    CommandKind.format, "dotnet format --verify-no-changes", ".", source
                ),
                _command(CommandKind.build, "dotnet build", ".", source),
                _command(CommandKind.test, "dotnet test", ".", source),
            ]
        )
        for path in paths:
            if Path(path).name == "Program.cs":
                out.entrypoints.append(
                    CodeSurface(
                        kind="dotnet_program",
                        path=path,
                        package_path=Path(path).parent.as_posix() or ".",
                    )
                )
        if out.dependencies and any(
            dependency.resolved_version is None for dependency in out.dependencies
        ):
            out.uncertainties.append(
                _uncertainty(
                    UncertaintyCode.unresolved_dependency_versions,
                    "Exact NuGet versions require packages.lock.json inspection.",
                    *out.lockfiles or [p.manifest_path for p in out.packages],
                )
            )
        return out


ADAPTERS: tuple[EcosystemAdapter, ...] = (
    NodeAdapter(),
    PythonAdapter(),
    GoAdapter(),
    RustAdapter(),
    JVMAdapter(),
    DotNetAdapter(),
)
