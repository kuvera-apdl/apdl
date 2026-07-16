"""Sandbox execution boundary tests for exact dependency contracts."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import app.contracts.installer as contract_installer
from app.contracts.cache import build_cache_identity
from app.contracts.installer import (
    BoundedCommandExecutor,
    CommandResult,
    SandboxedCheckRunner,
    SandboxedInstallRunner,
    detect_contract_input_drift,
    locate_python_site_packages,
)
from app.contracts.models import (
    BlockerCode,
    ContractBlocker,
    ContractCheckRequest,
    ContractCheckResult,
    ContractCheckStatus,
    ContractInstallRequest,
    ContractRequest,
    ContractResolution,
    RuntimeFingerprint,
)


def _write(root: Path, path: str, text: str = "") -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _runtime(name: str = "node", version: str = "20.18.0") -> RuntimeFingerprint:
    return RuntimeFingerprint(
        runtime_name=name,
        runtime_version=version,
        operating_system="linux",
        architecture="x86_64",
    )


def _install_request(
    repo: Path,
    *,
    ecosystem: str = "node",
    manifest: str = "package.json",
    lockfile: str = "package-lock.json",
) -> ContractInstallRequest:
    return ContractInstallRequest(
        repository_root=repo.as_posix(),
        request=ContractRequest(
            ecosystem=ecosystem,
            package_path=".",
            package_name="example-sdk",
            exact_version="1.2.3",
            manifest_path=manifest,
            lockfile_path=lockfile,
        ),
        runtime=_runtime("python", "3.12.8") if ecosystem == "python" else _runtime(),
    )


class CaptureExecutor:
    def __init__(self, result: CommandResult | None = None) -> None:
        self.result = result or CommandResult(returncode=0, output="ok")
        self.calls: list[dict] = []

    def __call__(
        self, argv, *, cwd, env, timeout_seconds, output_limit
    ) -> CommandResult:
        self.calls.append(
            {
                "argv": tuple(argv),
                "cwd": cwd,
                "env": dict(env),
                "timeout": timeout_seconds,
                "output_limit": output_limit,
                "example_exists": Path(argv[-1]).is_file() if argv else False,
            }
        )
        return self.result


@pytest.mark.parametrize(
    ("lockfile", "prefix", "required"),
    [
        ("package-lock.json", ("npm", "ci"), "--ignore-scripts"),
        ("npm-shrinkwrap.json", ("npm", "ci"), "--ignore-scripts"),
        ("pnpm-lock.yaml", ("corepack", "pnpm", "install"), "--frozen-lockfile"),
        ("yarn.lock", ("corepack", "yarn", "install"), "--immutable"),
        ("bun.lock", ("bun", "install"), "--frozen-lockfile"),
        ("bun.lockb", ("bun", "install"), "--ignore-scripts"),
    ],
)
def test_node_lockfiles_map_to_frozen_scriptless_commands(
    tmp_path, lockfile, prefix, required
):
    repo = tmp_path / "repo"
    work = tmp_path / "work"
    work.mkdir()
    _write(repo, "package.json", "{}")
    _write(repo, lockfile, "lock")
    executor = CaptureExecutor()
    result = SandboxedInstallRunner(
        sandboxed=True, executor=executor, workdir_base=work
    )(_install_request(repo, lockfile=lockfile))

    assert result.status == "installed"
    argv = executor.calls[0]["argv"]
    assert argv[: len(prefix)] == prefix
    assert required in argv
    assert "--ignore-scripts" in argv or "--mode=skip-builds" in argv


def test_installer_refuses_to_execute_without_explicit_sandbox(tmp_path):
    repo = tmp_path / "repo"
    _write(repo, "package.json", "{}")
    _write(repo, "package-lock.json", "{}")
    executor = CaptureExecutor()

    result = SandboxedInstallRunner(sandboxed=False, executor=executor)(
        _install_request(repo)
    )

    assert result.status == "unsupported"
    assert "refused" in (result.message or "")
    assert executor.calls == []


def test_install_environment_does_not_inherit_application_or_model_secrets(
    monkeypatch, tmp_path
):
    for name in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "APDL_INTERNAL_TOKEN",
        "POSTGRES_URL",
        "DATABASE_URL",
    ):
        monkeypatch.setenv(name, f"secret-{name}")
    repo = tmp_path / "repo"
    work = tmp_path / "work"
    work.mkdir()
    _write(repo, "package.json", "{}")
    _write(repo, "package-lock.json", "{}")
    executor = CaptureExecutor()

    SandboxedInstallRunner(sandboxed=True, executor=executor, workdir_base=work)(
        _install_request(repo)
    )

    environment = executor.calls[0]["env"]
    assert "PATH" in environment
    assert (
        not {
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "APDL_INTERNAL_TOKEN",
            "POSTGRES_URL",
            "DATABASE_URL",
        }
        & environment.keys()
    )


def test_python_uv_uses_frozen_no_build_install_into_isolated_environment(tmp_path):
    repo = tmp_path / "repo"
    work = tmp_path / "work"
    work.mkdir()
    _write(repo, "pyproject.toml", "[project]\nname='demo'")
    _write(repo, "uv.lock", "version=1")
    executor = CaptureExecutor()

    result = SandboxedInstallRunner(
        sandboxed=True, executor=executor, workdir_base=work
    )(
        _install_request(
            repo,
            ecosystem="python",
            manifest="pyproject.toml",
            lockfile="uv.lock",
        )
    )

    assert result.status == "installed"
    assert executor.calls[0]["argv"] == (
        "uv",
        "sync",
        "--frozen",
        "--no-install-project",
        "--no-build",
    )
    assert executor.calls[0]["env"]["UV_PROJECT_ENVIRONMENT"] == result.installed_root


def test_unknown_lockfile_and_failed_command_are_explicit_results(tmp_path):
    repo = tmp_path / "repo"
    work = tmp_path / "work"
    work.mkdir()
    _write(repo, "package.json", "{}")
    _write(repo, "unknown.lock", "lock")
    unsupported = SandboxedInstallRunner(
        sandboxed=True, executor=CaptureExecutor(), workdir_base=work
    )(_install_request(repo, lockfile="unknown.lock"))
    assert unsupported.status == "unsupported"

    _write(repo, "package-lock.json", "{}")
    failed = SandboxedInstallRunner(
        sandboxed=True,
        executor=CaptureExecutor(CommandResult(returncode=2, output="registry failed")),
        output_limit=100,
        workdir_base=work,
    )(_install_request(repo))
    assert failed.status == "failed"
    assert "registry failed" in (failed.message or "")


def test_typescript_checker_uses_installed_compiler_and_zero_exit_only(
    monkeypatch, tmp_path
):
    installed = tmp_path / "installed"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(
        contract_installer,
        "_trusted_root_executable",
        lambda _name: Path(sys.executable),
    )
    compiler = _write(installed, "node_modules/typescript/bin/tsc", "binary")
    _write(installed, "node_modules/typescript/package.json", '{"version":"5.7.2"}')
    executor = CaptureExecutor(CommandResult(returncode=0, output="clean"))
    request = ContractCheckRequest(
        ecosystem="node",
        package_name="example-sdk",
        exact_version="1.2.3",
        installed_root=installed.as_posix(),
        language="TypeScript",
        snippet='import { Client } from "example-sdk";\nvoid Client;',
    )

    passed = SandboxedCheckRunner(sandboxed=True, executor=executor, workdir_base=work)(
        request
    )

    assert passed.status is ContractCheckStatus.passed
    assert passed.tool_version == "5.7.2"
    assert Path(executor.calls[0]["argv"][0]).is_absolute()
    assert executor.calls[0]["argv"][1] == compiler.as_posix()
    assert "--noEmit" in executor.calls[0]["argv"]
    assert executor.calls[0]["example_exists"] is True
    assert not Path(executor.calls[0]["argv"][-1]).exists()

    failed = SandboxedCheckRunner(
        sandboxed=True,
        executor=CaptureExecutor(CommandResult(returncode=1, output="bad import")),
        workdir_base=work,
    )(request)
    assert failed.status is ContractCheckStatus.failed


@pytest.mark.parametrize(
    "command_result",
    [
        CommandResult(returncode=127, output="permission denied", started=False),
        CommandResult(returncode=124, output="timed out", timed_out=True),
        CommandResult(returncode=126, output="cannot execute"),
    ],
)
def test_checker_execution_failures_are_unavailable(
    monkeypatch, tmp_path, command_result
):
    installed = tmp_path / "installed"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(
        contract_installer,
        "_trusted_root_executable",
        lambda _name: Path(sys.executable),
    )
    _write(installed, "node_modules/typescript/bin/tsc", "binary")
    _write(installed, "node_modules/typescript/package.json", '{"version":"5.7.2"}')

    result = SandboxedCheckRunner(
        sandboxed=True,
        executor=CaptureExecutor(command_result),
        workdir_base=work,
    )(
        ContractCheckRequest(
            ecosystem="node",
            package_name="example-sdk",
            exact_version="1.2.3",
            installed_root=installed.as_posix(),
            language="TypeScript",
            snippet="const value = 1;",
        )
    )

    assert result.status is ContractCheckStatus.unavailable


def test_contract_check_result_rejects_legacy_boolean_schema():
    with pytest.raises(ValueError):
        ContractCheckResult.model_validate(
            {
                "schema_version": "contract_check_result@1",
                "passed": True,
                "command": "tsc --noEmit",
                "tool_version": "5.7.2",
            }
        )


def test_checker_refuses_without_explicit_sandbox(tmp_path):
    installed = tmp_path / "installed"
    installed.mkdir()
    executor = CaptureExecutor()
    result = SandboxedCheckRunner(sandboxed=False, executor=executor)(
        ContractCheckRequest(
            ecosystem="node",
            package_name="example-sdk",
            exact_version="1.2.3",
            installed_root=installed.as_posix(),
            language="TypeScript",
            snippet="const value = 1;",
        )
    )
    assert result.status is ContractCheckStatus.unavailable
    assert result.command == "sandbox-required"
    assert executor.calls == []


def test_installer_converts_executor_fault_to_explicit_failure(tmp_path):
    repo = tmp_path / "repo"
    work = tmp_path / "work"
    work.mkdir()
    _write(repo, "package.json", "{}")
    _write(repo, "package-lock.json", "{}")

    def broken(*_args, **_kwargs):
        raise OSError("package manager missing")

    result = SandboxedInstallRunner(sandboxed=True, executor=broken, workdir_base=work)(
        _install_request(repo)
    )
    assert result.status == "failed"
    assert "package manager missing" in (result.message or "")


def test_python_checker_locates_site_packages_without_importing(tmp_path):
    installed = tmp_path / "venv"
    work = tmp_path / "work"
    work.mkdir()
    checker = _write(installed, "bin/pyright", "binary")
    site = installed / "lib/python3.12/site-packages"
    _write(
        installed,
        "lib/python3.12/site-packages/pyright-1.1.390.dist-info/METADATA",
        "Name: pyright\nVersion: 1.1.390\n",
    )
    _write(
        installed,
        "lib/python3.12/site-packages/example_sdk/__init__.py",
        "raise RuntimeError('must not import')\n",
    )
    executor = CaptureExecutor()

    result = SandboxedCheckRunner(sandboxed=True, executor=executor, workdir_base=work)(
        ContractCheckRequest(
            ecosystem="python",
            package_name="example-sdk",
            exact_version="1.2.3",
            installed_root=installed.as_posix(),
            language="Python",
            snippet="from example_sdk import Client\n_ = Client",
        )
    )

    assert locate_python_site_packages(installed) == site.resolve()
    assert result.status is ContractCheckStatus.passed
    assert result.tool_version == "1.1.390"
    assert Path(executor.calls[0]["argv"][0]).is_absolute()
    assert executor.calls[0]["argv"][1] == checker.as_posix()
    assert executor.calls[0]["env"]["PYTHONPATH"] == site.as_posix()


def test_bounded_executor_caps_output_and_kills_timeout(tmp_path):
    executor = BoundedCommandExecutor()
    environment = {"PATH": str(Path(sys.executable).parent)}
    capped = executor(
        [sys.executable, "-c", "print('x' * 10000)"],
        cwd=tmp_path,
        env=environment,
        timeout_seconds=2,
        output_limit=100,
    )
    assert capped.returncode == 0
    assert capped.output_truncated is True
    assert len(capped.output) < 200

    timed_out = executor(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        cwd=tmp_path,
        env=environment,
        timeout_seconds=0.05,
        output_limit=100,
    )
    assert timed_out.returncode == 124
    assert timed_out.timed_out is True


def test_manifest_and_lockfile_drift_are_detected_from_cache_identity(tmp_path):
    _write(tmp_path, "package.json", "{}")
    _write(tmp_path, "package-lock.json", "{}")
    request = _install_request(tmp_path).request
    identity = build_cache_identity(
        tmp_path,
        project_scope="project-a",
        repository="acme/app",
        request=request,
        runtime=_runtime(),
        extractor_version="extractor@1",
    )
    resolution = ContractResolution(
        request=request,
        cache_identity=identity,
        disposition="blocked",
        blockers=[
            ContractBlocker(
                code=BlockerCode.install_failed,
                package_name="example-sdk",
                message="fixture",
            )
        ],
    )
    assert detect_contract_input_drift(tmp_path, resolution) is None

    _write(tmp_path, "package-lock.json", '{"changed":true}')
    drift = detect_contract_input_drift(tmp_path, resolution)
    assert drift is not None
    assert drift.changed_paths == ("package-lock.json",)
