"""Resolver blockers and installed-tree runner seams."""

from app.contracts.cache import MemoryContractCache
from app.contracts.models import (
    BlockerCode,
    ContractCheckResult,
    ContractCheckStatus,
    ContractInstallResult,
    ContractRequest,
    RuntimeFingerprint,
)
from app.contracts.resolver import resolve_contract_request


def _write(root, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _runtime() -> RuntimeFingerprint:
    return RuntimeFingerprint(
        runtime_name="node",
        runtime_version="20.18.0",
        operating_system="linux",
        architecture="x86_64",
    )


def _request(ecosystem="node") -> ContractRequest:
    return ContractRequest(
        ecosystem=ecosystem,
        package_path=".",
        package_name="pkg",
        exact_version="1.0.0",
        manifest_path="package.json",
        lockfile_path="package-lock.json",
    )


def _resolve(repo, request, installer):
    return resolve_contract_request(
        repo,
        project_scope="project-a",
        repository="acme/app",
        request=request,
        runtime=_runtime(),
        install_runner=installer,
    )


def _passing_check(_request):
    return ContractCheckResult(
        status=ContractCheckStatus.passed,
        command="/usr/bin/node /workspace/tsc --noEmit",
        tool_version="5.7.2",
    )


def test_unsupported_ecosystem_is_explicit_and_never_installs(tmp_path):
    _write(tmp_path, "package.json", "{}")
    _write(tmp_path, "package-lock.json", "{}")
    called = False

    def installer(_request):
        nonlocal called
        called = True
        raise AssertionError("must not install")

    resolution = _resolve(tmp_path, _request("rust"), installer)
    assert resolution.disposition == "blocked"
    assert resolution.blockers[0].code is BlockerCode.unsupported_ecosystem
    assert called is False


def test_install_failure_is_explicit(tmp_path):
    _write(tmp_path, "package.json", "{}")
    _write(tmp_path, "package-lock.json", "{}")

    resolution = _resolve(
        tmp_path,
        _request(),
        lambda _request: ContractInstallResult(
            status="failed", message="registry unavailable"
        ),
    )

    assert resolution.disposition == "blocked"
    assert resolution.blockers[0].code is BlockerCode.install_failed
    assert "registry unavailable" in resolution.blockers[0].message


def test_missing_install_runner_is_an_unsupported_toolchain_blocker(tmp_path):
    _write(tmp_path, "package.json", "{}")
    _write(tmp_path, "package-lock.json", "{}")
    resolution = _resolve(tmp_path, _request(), None)
    assert resolution.blockers[0].code is BlockerCode.unsupported_toolchain


def test_ready_resolution_is_reused_by_exact_cache_identity(tmp_path):
    repo = tmp_path / "repo"
    installed = tmp_path / "installed"
    _write(repo, "package.json", "{}")
    _write(repo, "package-lock.json", "{}")
    _write(
        installed,
        "node_modules/pkg/package.json",
        '{"name":"pkg","version":"1.0.0","types":"index.d.ts"}',
    )
    _write(installed, "node_modules/pkg/index.d.ts", "export const value: string;")
    cache = MemoryContractCache()
    installs = 0

    def installer(_request):
        nonlocal installs
        installs += 1
        return ContractInstallResult(
            status="installed", installed_root=installed.as_posix()
        )

    first = resolve_contract_request(
        repo,
        project_scope="project-a",
        repository="acme/app",
        request=_request(),
        runtime=_runtime(),
        install_runner=installer,
        check_runner=_passing_check,
        cache=cache,
    )
    second = resolve_contract_request(
        repo,
        project_scope="project-a",
        repository="acme/app",
        request=_request(),
        runtime=_runtime(),
        install_runner=lambda _request: (_ for _ in ()).throw(
            AssertionError("cache hit must skip installation")
        ),
        check_runner=_passing_check,
        cache=cache,
    )

    assert first.disposition == "ready"
    assert second == first
    assert installs == 1


def test_malformed_installed_metadata_is_an_inspection_blocker(tmp_path):
    repo = tmp_path / "repo"
    installed = tmp_path / "installed"
    _write(repo, "package.json", "{}")
    _write(repo, "package-lock.json", "{}")
    _write(installed, "node_modules/pkg/package.json", "not-json")

    resolution = resolve_contract_request(
        repo,
        project_scope="project-a",
        repository="acme/app",
        request=_request(),
        runtime=_runtime(),
        install_runner=lambda _request: ContractInstallResult(
            status="installed", installed_root=installed.as_posix()
        ),
    )

    assert resolution.disposition == "blocked"
    assert resolution.blockers[0].code is BlockerCode.inspection_failed


def test_unavailable_compile_check_blocks_contract_resolution(tmp_path):
    repo = tmp_path / "repo"
    installed = tmp_path / "installed"
    _write(repo, "package.json", "{}")
    _write(repo, "package-lock.json", "{}")
    _write(
        installed,
        "node_modules/pkg/package.json",
        '{"name":"pkg","version":"1.0.0","types":"index.d.ts"}',
    )
    _write(installed, "node_modules/pkg/index.d.ts", "export const value: string;")

    def unavailable(_request):
        return ContractCheckResult(
            status=ContractCheckStatus.unavailable,
            command="/usr/bin/node /workspace/tsc --noEmit",
            tool_version="unavailable",
            output="permission denied",
        )

    resolution = resolve_contract_request(
        repo,
        project_scope="project-a",
        repository="acme/app",
        request=_request(),
        runtime=_runtime(),
        install_runner=lambda _request: ContractInstallResult(
            status="installed", installed_root=installed.as_posix()
        ),
        check_runner=unavailable,
    )

    assert resolution.disposition == "blocked"
    assert resolution.evidence is None
    assert resolution.blockers[0].code is BlockerCode.compile_check_unavailable
