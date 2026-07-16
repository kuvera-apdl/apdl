"""Sandbox-only dependency installation and example checking.

This module does not create the isolation boundary itself.  Callers must place
it inside the hardened worker and opt in with ``sandboxed=True``.  The explicit
flag prevents an accidental integration from executing customer manifests in
the API process.  Commands never use a shell, receive a fresh environment, and
are bounded by wall time and captured-output size.
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Protocol

from app.contracts.cache import sha256_file
from app.contracts.models import (
    ContractCheckRequest,
    ContractCheckResult,
    ContractCheckStatus,
    ContractInstallRequest,
    ContractInstallResult,
    ContractResolution,
)


_DEFAULT_TIMEOUT_SECONDS = 300.0
_DEFAULT_OUTPUT_LIMIT = 32_000
_TRUSTED_SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_SECRET_MARKERS = (
    "ANTHROPIC",
    "APDL",
    "DATABASE",
    "GEMINI",
    "GITHUB",
    "GOOGLE",
    "OPENAI",
    "POSTGRES",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str = ""
    timed_out: bool = False
    output_truncated: bool = False
    started: bool = True


class CommandExecutor(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
        output_limit: int,
    ) -> CommandResult: ...


def _secret_name(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in _SECRET_MARKERS)


def sanitized_environment(
    *, home: Path, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build a fresh subprocess environment with no application/model secrets."""
    environment = {
        "PATH": _TRUSTED_SYSTEM_PATH,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "HOME": home.as_posix(),
        "TMPDIR": home.as_posix(),
        "CI": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_IGNORE_SCRIPTS": "true",
        "NPM_CONFIG_USERCONFIG": os.devnull,
        "PIP_CONFIG_FILE": os.devnull,
        "PYTHONNOUSERSITE": "1",
    }
    for name, value in (extra or {}).items():
        if _secret_name(name):
            raise ValueError(f"Refusing secret-like environment field: {name}")
        environment[name] = value
    return environment


class BoundedCommandExecutor:
    """Execute one argv without a shell, bounding time and retained output."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
        output_limit: int,
    ) -> CommandResult:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if output_limit <= 0:
            raise ValueError("output_limit must be positive")
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            return CommandResult(returncode=127, output=str(exc), started=False)
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        retained = bytearray()
        total = 0
        timed_out = False
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                for key, _mask in selector.select(timeout=min(remaining, 0.1)):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if len(retained) < output_limit:
                        retained.extend(chunk[: output_limit - len(retained)])
                if process.poll() is not None and not selector.get_map():
                    break
            if timed_out:
                process.wait()
            else:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        finally:
            selector.close()
            process.stdout.close()
        truncated = total > len(retained)
        output = retained.decode("utf-8", "replace")
        if truncated:
            output += f"\n[…output truncated after {output_limit} bytes…]"
        if timed_out:
            output += f"\n[…command timed out after {timeout_seconds:g}s…]"
        return CommandResult(
            returncode=124 if timed_out else int(process.returncode or 0),
            output=output,
            timed_out=timed_out,
            output_truncated=truncated,
        )


@dataclass(frozen=True)
class _InstallPlan:
    commands: tuple[tuple[str, ...], ...]
    cwd: Path
    installed_root: Path
    environment: Mapping[str, str]


def _safe_repo_path(repository_root: Path, relative: str) -> Path | None:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        return None
    candidate = repository_root.joinpath(*pure.parts)
    try:
        candidate.resolve().relative_to(repository_root.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def _node_install_plan(
    request: ContractInstallRequest, *, home: Path
) -> _InstallPlan | None:
    repo = Path(request.repository_root)
    lockfile_name = Path(request.request.lockfile_path or "").name
    lockfile = _safe_repo_path(repo, request.request.lockfile_path or "")
    manifest = _safe_repo_path(repo, request.request.manifest_path)
    if (
        lockfile is None
        or manifest is None
        or not lockfile.is_file()
        or not manifest.is_file()
    ):
        return None
    commands: dict[str, tuple[str, ...]] = {
        "package-lock.json": (
            "npm",
            "ci",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        ),
        "npm-shrinkwrap.json": (
            "npm",
            "ci",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        ),
        "pnpm-lock.yaml": (
            "corepack",
            "pnpm",
            "install",
            "--frozen-lockfile",
            "--ignore-scripts",
        ),
        "yarn.lock": (
            "corepack",
            "yarn",
            "install",
            "--immutable",
            "--mode=skip-builds",
        ),
        "bun.lock": ("bun", "install", "--frozen-lockfile", "--ignore-scripts"),
        "bun.lockb": ("bun", "install", "--frozen-lockfile", "--ignore-scripts"),
    }
    argv = commands.get(lockfile_name)
    if argv is None:
        return None
    return _InstallPlan(
        commands=(argv,),
        cwd=lockfile.parent,
        installed_root=lockfile.parent,
        environment=sanitized_environment(home=home),
    )


def _python_install_plan(
    request: ContractInstallRequest, *, home: Path, environment_root: Path
) -> _InstallPlan | None:
    repo = Path(request.repository_root)
    lockfile_name = Path(request.request.lockfile_path or "").name
    lockfile = _safe_repo_path(repo, request.request.lockfile_path or "")
    manifest = _safe_repo_path(repo, request.request.manifest_path)
    if (
        lockfile is None
        or manifest is None
        or not lockfile.is_file()
        or not manifest.is_file()
    ):
        return None
    base_extra = {"VIRTUAL_ENV": environment_root.as_posix()}
    if lockfile_name == "uv.lock":
        commands = (("uv", "sync", "--frozen", "--no-install-project", "--no-build"),)
        extra = {**base_extra, "UV_PROJECT_ENVIRONMENT": environment_root.as_posix()}
        installed_root = environment_root
    elif lockfile_name == "poetry.lock":
        commands = (
            ("python", "-m", "venv", environment_root.as_posix()),
            ("poetry", "install", "--no-root", "--sync", "--no-interaction"),
        )
        extra = {**base_extra, "POETRY_VIRTUALENVS_CREATE": "false"}
        installed_root = environment_root
    elif lockfile_name == "pdm.lock":
        commands = (
            ("python", "-m", "venv", environment_root.as_posix()),
            ("pdm", "sync", "--frozen-lockfile", "--no-self", "--no-editable"),
        )
        extra = base_extra
        installed_root = environment_root
    elif lockfile_name.startswith("requirements") and lockfile_name.endswith(".txt"):
        target = environment_root / "site-packages"
        target.mkdir(parents=True, exist_ok=True)
        commands = (
            (
                "python",
                "-m",
                "pip",
                "install",
                "--require-hashes",
                "--only-binary=:all:",
                "--target",
                target.as_posix(),
                "-r",
                lockfile.as_posix(),
            ),
        )
        extra = {}
        installed_root = target
    else:
        return None
    return _InstallPlan(
        commands=commands,
        cwd=lockfile.parent,
        installed_root=installed_root,
        environment=sanitized_environment(home=home, extra=extra),
    )


class SandboxedInstallRunner:
    """Install exact Node/Python locks only after an explicit sandbox opt-in."""

    def __init__(
        self,
        *,
        sandboxed: bool,
        executor: CommandExecutor | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        output_limit: int = _DEFAULT_OUTPUT_LIMIT,
        workdir_base: Path | None = None,
    ) -> None:
        self._sandboxed = sandboxed
        self._executor = executor or BoundedCommandExecutor()
        self._timeout = timeout_seconds
        self._output_limit = output_limit
        self._workdir_base = workdir_base

    def __call__(self, request: ContractInstallRequest) -> ContractInstallResult:
        if not self._sandboxed:
            return ContractInstallResult(
                status="unsupported",
                message="Dependency installation is refused outside an explicit sandbox.",
            )
        work = Path(
            tempfile.mkdtemp(prefix="apdl-contract-install-", dir=self._workdir_base)
        )
        home = work / "home"
        environment_root = work / "environment"
        home.mkdir(mode=0o700)
        ecosystem = request.request.ecosystem.casefold()
        if ecosystem == "node":
            plan = _node_install_plan(request, home=home)
        elif ecosystem == "python":
            plan = _python_install_plan(
                request,
                home=home,
                environment_root=environment_root,
            )
        else:
            shutil.rmtree(work, ignore_errors=True)
            return ContractInstallResult(
                status="unsupported",
                message=f"Sandbox installer does not support {request.request.ecosystem}.",
            )
        if plan is None:
            shutil.rmtree(work, ignore_errors=True)
            return ContractInstallResult(
                status="unsupported",
                message=(
                    "No canonical frozen/immutable install command exists for "
                    f"lockfile {request.request.lockfile_path!r}."
                ),
            )
        for argv in plan.commands:
            try:
                result = self._executor(
                    argv,
                    cwd=plan.cwd,
                    env=plan.environment,
                    timeout_seconds=self._timeout,
                    output_limit=self._output_limit,
                )
            except Exception as exc:
                shutil.rmtree(work, ignore_errors=True)
                return ContractInstallResult(
                    status="failed",
                    message=f"Sandboxed dependency installer failed: {exc}",
                )
            if result.returncode != 0:
                reason = (
                    "timed out" if result.timed_out else f"exited {result.returncode}"
                )
                failure = ContractInstallResult(
                    status="failed",
                    message=(
                        f"Sandboxed dependency install {reason}: {result.output}"
                    ).strip(),
                )
                shutil.rmtree(work, ignore_errors=True)
                return failure
        installed = ContractInstallResult(
            status="installed", installed_root=plan.installed_root.as_posix()
        )
        if not plan.installed_root.resolve().is_relative_to(work.resolve()):
            shutil.rmtree(work, ignore_errors=True)
        return installed


def locate_python_site_packages(installed_root: Path) -> Path | None:
    """Locate a site-packages directory without importing the environment."""
    candidates = [
        installed_root,
        installed_root / "Lib" / "site-packages",
        *sorted((installed_root / "lib").glob("python*/site-packages")),
    ]
    root = installed_root.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if not resolved.is_dir():
            continue
        if resolved.name == "site-packages" or any(resolved.glob("*.dist-info")):
            return resolved
    return None


def _distribution_version(site_packages: Path, package_name: str) -> str | None:
    normalized = package_name.replace("_", "-").casefold()
    for metadata in sorted(site_packages.glob("*.dist-info/METADATA")):
        try:
            resolved = metadata.resolve()
            resolved.relative_to(site_packages.resolve())
            parsed = Parser().parsestr(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = parsed.get("Name", "").replace("_", "-").casefold()
        if name == normalized:
            return parsed.get("Version") or None
    return None


def _safe_executable(installed_root: Path, relative: str) -> Path | None:
    candidate = installed_root / relative
    try:
        candidate.resolve().relative_to(installed_root.resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _trusted_root_executable(name: str) -> Path | None:
    candidate = shutil.which(name, path=_TRUSTED_SYSTEM_PATH)
    if candidate is None:
        return None
    try:
        resolved = Path(candidate).resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_absolute() and resolved.is_file() else None


def _root_python() -> Path | None:
    try:
        resolved = Path(sys.executable).resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_absolute() and resolved.is_file() else None


class SandboxedCheckRunner:
    """Compile/typecheck examples statically; zero exit is the only pass."""

    def __init__(
        self,
        *,
        sandboxed: bool,
        executor: CommandExecutor | None = None,
        timeout_seconds: float = 60.0,
        output_limit: int = _DEFAULT_OUTPUT_LIMIT,
        workdir_base: Path | None = None,
    ) -> None:
        self._sandboxed = sandboxed
        self._executor = executor or BoundedCommandExecutor()
        self._timeout = timeout_seconds
        self._output_limit = output_limit
        self._workdir_base = workdir_base

    def _failure(self, command: str, output: str) -> ContractCheckResult:
        return ContractCheckResult(
            status=ContractCheckStatus.unavailable,
            command=command,
            tool_version="unavailable",
            output=output,
        )

    @staticmethod
    def _result(
        *,
        result: CommandResult,
        command: str,
        tool_version: str,
    ) -> ContractCheckResult:
        if (
            not result.started
            or result.timed_out
            or result.returncode < 0
            or result.returncode in {126, 127}
        ):
            status = ContractCheckStatus.unavailable
        elif result.returncode == 0:
            status = ContractCheckStatus.passed
        else:
            status = ContractCheckStatus.failed
        return ContractCheckResult(
            status=status,
            command=command,
            tool_version=tool_version,
            output=result.output,
        )

    def __call__(self, request: ContractCheckRequest) -> ContractCheckResult:
        if not self._sandboxed:
            return self._failure(
                "sandbox-required",
                "Example checking is refused outside an explicit sandbox.",
            )
        installed_root = Path(request.installed_root)
        if not installed_root.is_dir():
            return self._failure("unavailable", "Installed tree does not exist.")
        with tempfile.TemporaryDirectory(
            prefix="apdl-contract-check-", dir=self._workdir_base
        ) as temp:
            work = Path(temp)
            home = work / "home"
            home.mkdir(mode=0o700)
            ecosystem = request.ecosystem.casefold()
            if ecosystem == "node":
                return self._check_node(request, installed_root, work, home)
            if ecosystem == "python":
                return self._check_python(request, installed_root, work, home)
            return self._failure(
                "unsupported",
                f"Example checker does not support {request.ecosystem}.",
            )

    def _check_node(
        self,
        request: ContractCheckRequest,
        installed_root: Path,
        work: Path,
        home: Path,
    ) -> ContractCheckResult:
        node = _trusted_root_executable("node")
        tsc = _safe_executable(installed_root, "node_modules/typescript/bin/tsc")
        metadata = _safe_repo_path(
            installed_root, "node_modules/typescript/package.json"
        )
        if node is None or tsc is None or metadata is None or not metadata.is_file():
            return self._failure(
                "tsc --noEmit", "Installed TypeScript compiler unavailable."
            )
        try:
            tool_version = str(
                json.loads(metadata.read_text(encoding="utf-8"))["version"]
            )
        except (OSError, ValueError, KeyError, TypeError):
            return self._failure(
                "tsc --noEmit", "TypeScript version metadata unavailable."
            )
        fd, example_name = tempfile.mkstemp(
            prefix=".apdl-contract-", suffix=".ts", dir=installed_root
        )
        example = Path(example_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(request.snippet)
            argv = (
                node.as_posix(),
                tsc.as_posix(),
                "--noEmit",
                "--skipLibCheck",
                "--pretty",
                "false",
                "--module",
                "node16",
                "--moduleResolution",
                "node16",
                example.as_posix(),
            )
            try:
                result = self._executor(
                    argv,
                    cwd=installed_root,
                    env=sanitized_environment(home=home),
                    timeout_seconds=self._timeout,
                    output_limit=self._output_limit,
                )
            except Exception as exc:
                return self._failure(
                    " ".join(argv), f"TypeScript checker failed: {exc}"
                )
        finally:
            example.unlink(missing_ok=True)
        command = " ".join(argv)
        return self._result(
            result=result,
            command=command,
            tool_version=tool_version,
        )

    def _check_python(
        self,
        request: ContractCheckRequest,
        installed_root: Path,
        work: Path,
        home: Path,
    ) -> ContractCheckResult:
        site_packages = locate_python_site_packages(installed_root)
        if site_packages is None:
            return self._failure("typecheck", "Python site-packages unavailable.")
        python = _root_python()
        if python is None:
            return self._failure("typecheck", "Root Python interpreter unavailable.")
        pyright = _safe_executable(installed_root, "bin/pyright")
        mypy = _safe_executable(installed_root, "bin/mypy")
        if pyright is not None:
            executable = pyright
            package_name = "pyright"
            argv_tail: tuple[str, ...] = ()
        elif mypy is not None:
            executable = mypy
            package_name = "mypy"
            argv_tail = ("--no-incremental", "--config-file", os.devnull)
        else:
            return self._failure("typecheck", "Installed pyright/mypy unavailable.")
        tool_version = _distribution_version(site_packages, package_name)
        if tool_version is None:
            return self._failure(
                executable.as_posix(), f"{package_name} version metadata unavailable."
            )
        example = work / "contract_example.py"
        example.write_text(request.snippet, encoding="utf-8")
        argv = (
            python.as_posix(),
            executable.as_posix(),
            *argv_tail,
            example.as_posix(),
        )
        try:
            result = self._executor(
                argv,
                cwd=work,
                env=sanitized_environment(
                    home=home, extra={"PYTHONPATH": site_packages.as_posix()}
                ),
                timeout_seconds=self._timeout,
                output_limit=self._output_limit,
            )
        except Exception as exc:
            return self._failure(" ".join(argv), f"Python checker failed: {exc}")
        command = " ".join(argv)
        return self._result(
            result=result,
            command=command,
            tool_version=tool_version,
        )


@dataclass(frozen=True)
class ContractInputDrift:
    package_name: str
    changed_paths: tuple[str, ...]


def detect_contract_input_drift(
    repository_root: Path, resolution: ContractResolution
) -> ContractInputDrift | None:
    """Detect manifest/lockfile changes after exact evidence was resolved."""
    identity = resolution.cache_identity
    if identity is None:
        return ContractInputDrift(
            package_name=resolution.request.package_name,
            changed_paths=(
                resolution.request.manifest_path,
                resolution.request.lockfile_path or "<missing-lockfile>",
            ),
        )
    changed: list[str] = []
    for relative, expected in (
        (identity.manifest_path, identity.manifest_sha256),
        (identity.lockfile_path, identity.lockfile_sha256),
    ):
        path = _safe_repo_path(repository_root, relative)
        if path is None or not path.is_file():
            changed.append(relative)
            continue
        try:
            actual = sha256_file(path)
        except OSError:
            changed.append(relative)
            continue
        if actual != expected:
            changed.append(relative)
    return (
        ContractInputDrift(
            package_name=resolution.request.package_name,
            changed_paths=tuple(sorted(changed)),
        )
        if changed
        else None
    )
