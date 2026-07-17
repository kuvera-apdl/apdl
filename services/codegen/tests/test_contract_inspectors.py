"""Installed-tree extraction and version-drift regression tests."""

import json

from app.contracts.cache import build_cache_identity
from app.contracts.inspectors import inspect_node_package, inspect_python_package
from app.contracts.models import (
    BlockerCode,
    ContractCheckResult,
    ContractCheckStatus,
    ContractRequest,
    RuntimeFingerprint,
)
from app.contracts.render import render_contract_bundle
from app.contracts.models import ContractBundle, ContractResolution


def _write(root, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _runtime(name: str, version: str) -> RuntimeFingerprint:
    return RuntimeFingerprint(
        runtime_name=name,
        runtime_version=version,
        operating_system="linux",
        architecture="x86_64",
    )


def _identity(repo, request, runtime):
    return build_cache_identity(
        repo,
        project_scope="project-a",
        repository="acme/app",
        request=request,
        runtime=runtime,
        extractor_version="extractor@1",
    )


def _passing_check(command: str, version: str):
    def run(_request):
        return ContractCheckResult(
            status=ContractCheckStatus.passed,
            command=command,
            tool_version=version,
            output="ok",
        )

    return run


def test_node_inspection_uses_installed_v1_types_not_head_documentation(tmp_path):
    repo = tmp_path / "repo"
    installed = tmp_path / "installed"
    _write(repo, "package.json", '{"dependencies":{"widget-sdk":"^1"}}')
    _write(repo, "package-lock.json", '{"widget-sdk":"1.4.0"}')
    # Simulates repository/package HEAD documentation advertising a newer API.
    _write(repo, "vendor-head/README.md", "widget-sdk v2 exports newApi()")
    _write(
        installed,
        "node_modules/widget-sdk/package.json",
        json.dumps(
            {
                "name": "widget-sdk",
                "version": "1.4.0",
                "types": "index.d.ts",
                "exports": {".": {"types": "./index.d.ts"}},
            }
        ),
    )
    _write(
        installed,
        "node_modules/widget-sdk/index.d.ts",
        "export declare function createClient(key: string): Promise<Client>;\n"
        "export interface Client { close(): void; }\n",
    )
    request = ContractRequest(
        ecosystem="node",
        package_path=".",
        package_name="widget-sdk",
        exact_version="1.4.0",
        manifest_path="package.json",
        lockfile_path="package-lock.json",
        symbols=["createClient", "newApi"],
    )

    evidence, blockers = inspect_node_package(
        installed,
        request,
        _identity(repo, request, _runtime("node", "20.18.0")),
        check_runner=_passing_check("tsc --noEmit", "5.7.2"),
    )

    assert evidence is not None
    assert evidence.exact_version == "1.4.0"
    assert [symbol.qualified_name for symbol in evidence.symbols] == ["createClient"]
    assert evidence.examples[0].check_result == "passed"
    assert all(len(source.sha256) == 64 for source in evidence.sources)
    assert all("vendor-head" not in source.relative_path for source in evidence.sources)
    assert BlockerCode.symbol_not_found in {item.code for item in blockers}
    rendered = render_contract_bundle(
        ContractBundle(
            resolutions=[
                ContractResolution(
                    request=request,
                    disposition="ready",
                    evidence=evidence,
                    blockers=blockers,
                )
            ]
        )
    )
    assert "createClient" in rendered
    assert "v2 exports newApi" not in rendered
    assert "not GitHub CI results" in rendered


def test_failed_node_example_is_not_emitted(tmp_path):
    repo = tmp_path / "repo"
    installed = tmp_path / "installed"
    _write(repo, "package.json", "{}")
    _write(repo, "package-lock.json", "{}")
    _write(
        installed,
        "node_modules/pkg/package.json",
        '{"name":"pkg","version":"1.0.0","types":"index.d.ts"}',
    )
    _write(installed, "node_modules/pkg/index.d.ts", "export const value: string;\n")
    request = ContractRequest(
        ecosystem="node",
        package_path=".",
        package_name="pkg",
        exact_version="1.0.0",
        manifest_path="package.json",
        lockfile_path="package-lock.json",
        symbols=["value"],
    )

    def failed(_request):
        return ContractCheckResult(
            status=ContractCheckStatus.failed,
            command="tsc --noEmit",
            tool_version="5.7.2",
            output="not exported",
        )

    evidence, blockers = inspect_node_package(
        installed,
        request,
        _identity(repo, request, _runtime("node", "20.18.0")),
        check_runner=failed,
    )
    assert evidence is not None and evidence.examples == []
    assert BlockerCode.example_check_failed in {item.code for item in blockers}


def test_python_inspection_reads_metadata_and_stubs_without_importing(tmp_path):
    repo = tmp_path / "repo"
    installed = tmp_path / "installed"
    _write(repo, "pyproject.toml", '[project]\ndependencies=["demo-sdk==2.3.1"]')
    _write(repo, "uv.lock", '[[package]]\nname="demo-sdk"\nversion="2.3.1"')
    site = "lib/python3.12/site-packages"
    _write(
        installed,
        f"{site}/demo_sdk-2.3.1.dist-info/METADATA",
        "Metadata-Version: 2.3\nName: demo-sdk\nVersion: 2.3.1\n",
    )
    _write(
        installed,
        f"{site}/demo_sdk-2.3.1.dist-info/top_level.txt",
        "demo_sdk\n",
    )
    _write(
        installed,
        f"{site}/demo_sdk/__init__.pyi",
        "async def connect(url: str) -> Client: ...\n"
        "class Client:\n    def close(self) -> None: ...\n",
    )
    # If inspection imported the package this would raise; AST/stub inspection
    # never evaluates it.
    _write(installed, f"{site}/demo_sdk/__init__.py", "raise RuntimeError('imported')")
    request = ContractRequest(
        ecosystem="python",
        package_path=".",
        package_name="demo-sdk",
        exact_version="2.3.1",
        manifest_path="pyproject.toml",
        lockfile_path="uv.lock",
        symbols=["connect", "Client.close"],
    )

    evidence, blockers = inspect_python_package(
        installed,
        request,
        _identity(repo, request, _runtime("python", "3.12.8")),
        check_runner=_passing_check("pyright example.py", "1.1.390"),
    )

    assert evidence is not None
    assert {symbol.qualified_name for symbol in evidence.symbols} == {
        "Client",
        "Client.close",
        "connect",
    }
    assert any("async def" in fact.statement for fact in evidence.lifecycle_facts)
    assert evidence.examples[0].check_result == "passed"
    assert not [item for item in blockers if item.severity == "blocking"]
