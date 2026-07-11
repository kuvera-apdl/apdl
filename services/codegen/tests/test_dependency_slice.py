"""Focused tests for cross-language dependency slicing and route evidence."""

from __future__ import annotations

from pathlib import Path

from app.inspection.models import EvidenceKind
from app.inspection.render import render_dependency_slice
from app.inspection.repository import RepositoryInspector
from app.inspection.slice import build_dependency_slice
from app.inspection.tracing import trace_local_imports


def _write(root: Path, path: str, content: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_dependency_slice_traces_imports_callers_routes_tests_and_lockfiles(
    tmp_path: Path,
):
    _write(
        tmp_path,
        "src/components/Button.tsx",
        "import { format } from '../lib/format'\n"
        "export const Button = () => <button>{format('Save')}</button>\n",
    )
    _write(tmp_path, "src/lib/format.ts", "export const format = (s: string) => s\n")
    _write(
        tmp_path,
        "app/page.tsx",
        "import { Button } from '../src/components/Button'\n"
        "export default function Page() {\n"
        '  return <><Button/><a href="/settings">Settings</a>'
        '<a href="/missing">Missing</a></>\n'
        "}\n",
    )
    _write(
        tmp_path,
        "app/settings/page.tsx",
        "export default function Settings() { return <main>Settings</main> }\n",
    )
    _write(
        tmp_path,
        "src/components/Button.test.tsx",
        "import { Button } from './Button'\n"
        "test('renders', () => expect(Button).toBeDefined())\n",
    )
    _write(tmp_path, "package-lock.json", '{"lockfileVersion": 3}\n')

    dependency_slice = build_dependency_slice(tmp_path, ["src/components/Button.tsx"])

    assert [item.path for item in dependency_slice.changed_files] == [
        "src/components/Button.tsx"
    ]
    assert "src/lib/format.ts" in {
        item.path for item in dependency_slice.imported_local_symbols
    }
    assert {item.path for item in dependency_slice.callers} >= {
        "app/page.tsx",
        "src/components/Button.test.tsx",
    }
    assert [item.path for item in dependency_slice.affected_tests] == [
        "src/components/Button.test.tsx"
    ]
    assert [item.path for item in dependency_slice.relevant_lockfiles] == [
        "package-lock.json"
    ]

    route_symbols = {
        item.symbol
        for item in dependency_slice.routes_and_handlers
        if item.kind is EvidenceKind.route
    }
    assert "/settings" in route_symbols
    assert any(
        item.kind is EvidenceKind.link
        and item.symbol == "/settings"
        and item.target_path == "app/settings/page.tsx"
        for item in dependency_slice.routes_and_handlers
    )
    assert dependency_slice.unresolved_references == ["app/page.tsx:3 -> /missing"]

    first_render = render_dependency_slice(dependency_slice)
    assert first_render == render_dependency_slice(dependency_slice)
    assert str(tmp_path) not in first_render
    assert '"schema_version": "dependency_slice@1"' in first_render


def test_import_tracing_covers_supported_language_families(tmp_path: Path):
    _write(tmp_path, "py/pkg/__init__.py", "")
    _write(tmp_path, "py/pkg/helpers.py", "def work(): pass\n")
    _write(tmp_path, "py/pkg/service.py", "from .helpers import work\n")

    _write(tmp_path, "go.mod", "module example.com/demo\n")
    _write(tmp_path, "internal/api/handler.go", "package api\n")
    _write(
        tmp_path,
        "cmd/server/main.go",
        'package main\nimport "example.com/demo/internal/api"\n',
    )

    _write(tmp_path, "Cargo.toml", '[package]\nname = "demo"\n')
    _write(tmp_path, "src/util.rs", "pub struct Thing;\n")
    _write(tmp_path, "src/lib.rs", "use crate::util::Thing;\n")

    _write(
        tmp_path,
        "src/main/java/com/acme/Util.java",
        "package com.acme; class Util {}\n",
    )
    _write(
        tmp_path,
        "src/main/java/com/acme/App.java",
        "package com.acme;\nimport com.acme.Util;\nclass App {}\n",
    )

    _write(tmp_path, "dotnet/Util.cs", "namespace Demo.Tools; public class Util {}\n")
    _write(
        tmp_path,
        "dotnet/App.cs",
        "using Demo.Tools;\nnamespace Demo.App; public class App {}\n",
    )

    collection = RepositoryInspector(tmp_path).collect_texts()
    edges = {
        (item.source_path, item.target_path) for item in trace_local_imports(collection)
    }

    assert ("py/pkg/service.py", "py/pkg/helpers.py") in edges
    assert ("cmd/server/main.go", "internal/api/handler.go") in edges
    assert ("src/lib.rs", "src/util.rs") in edges
    assert (
        "src/main/java/com/acme/App.java",
        "src/main/java/com/acme/Util.java",
    ) in edges
    assert ("dotnet/App.cs", "dotnet/Util.cs") in edges


def test_deleted_changed_file_remains_traceable_and_group_caps_are_explicit(
    tmp_path: Path,
):
    _write(tmp_path, "src/a.py", "from src.b import value\n")
    _write(tmp_path, "src/b.py", "value = 1\n")

    complete_slice = build_dependency_slice(
        tmp_path,
        ["src/deleted.py", "src/a.py"],
    )
    deleted = next(
        item for item in complete_slice.changed_files if item.path == "src/deleted.py"
    )
    assert deleted.excerpt is None
    assert len(deleted.content_sha256) == 64

    dependency_slice = build_dependency_slice(
        tmp_path,
        ["src/deleted.py", "src/a.py"],
        max_evidence_per_group=1,
    )

    assert len(dependency_slice.changed_files) == 1
    assert dependency_slice.truncated is True
    # Rebuilding from the same repository state yields byte-for-byte stable IDs.
    assert dependency_slice == build_dependency_slice(
        tmp_path,
        ["src/deleted.py", "src/a.py"],
        max_evidence_per_group=1,
    )
