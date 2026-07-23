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
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from app.contracts.cache import sha256_file
from app.contracts.image_checker import (
    IMAGE_NODE_MODULES,
    PYRIGHT_ENTRYPOINT,
    PYRIGHT_PACKAGE,
    PYRIGHT_VERSION,
    TYPESCRIPT_ENTRYPOINT,
    TYPESCRIPT_PACKAGE,
    TYPESCRIPT_VERSION,
    pyright_config,
    typescript_config,
)
from app.contracts.models import (
    ContractCheckRequest,
    ContractCheckResult,
    ContractCheckStatus,
    ContractInstallRequest,
    ContractInstallResult,
    ContractResolution,
)
from app.egress import inherited_proxy_environment
from app.safety.secrets import secret_environment_name


_DEFAULT_TIMEOUT_SECONDS = 300.0
_DEFAULT_OUTPUT_LIMIT = 32_000
_TRUSTED_SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


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
    environment.update(inherited_proxy_environment())
    for name, value in (extra or {}).items():
        if secret_environment_name(name):
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


def _trusted_root_executable(name: str) -> Path | None:
    candidate = shutil.which(name, path=_TRUSTED_SYSTEM_PATH)
    if candidate is None:
        return None
    try:
        resolved = Path(candidate).resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_absolute() and resolved.is_file() else None


def _image_node_entrypoint(
    package: str,
    relative_entrypoint: Path,
    expected_version: str,
) -> Path | None:
    """Resolve one exact image-owned Node tool without consulting PATH or repos."""
    try:
        modules_root = IMAGE_NODE_MODULES.resolve(strict=True)
        package_root = (modules_root / package).resolve(strict=True)
        package_root.relative_to(modules_root)
        package_json = (package_root / "package.json").resolve(strict=True)
        package_json.relative_to(package_root)
        entrypoint = (package_root / relative_entrypoint).resolve(strict=True)
        entrypoint.relative_to(package_root)
        metadata = json.loads(package_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != package
        or metadata.get("version") != expected_version
        or not entrypoint.is_file()
    ):
        return None
    return entrypoint


class ImageOwnedCheckRunner:
    """Semantically check examples with image-owned compilers.

    The exact installed dependency tree is readable compiler input.  Nothing
    from that tree is selected as an executable, compiler config, plugin, or
    environment source.
    """

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
        language = request.language.casefold()
        suffixes = {
            "javascript": (".js", True),
            "typescript": (".ts", False),
            "tsx": (".tsx", False),
        }
        language_settings = suffixes.get(language)
        if language_settings is None:
            return self._failure(
                "image-owned TypeScript semantic check",
                f"Unsupported Node example language: {request.language}.",
            )
        node = _trusted_root_executable("node")
        if node is None:
            return self._failure(
                "image-owned TypeScript semantic check",
                "Image-owned Node is unavailable.",
            )
        compiler = _image_node_entrypoint(
            TYPESCRIPT_PACKAGE,
            TYPESCRIPT_ENTRYPOINT,
            TYPESCRIPT_VERSION,
        )
        if compiler is None:
            return self._failure(
                "image-owned TypeScript semantic check",
                "The exact image-owned TypeScript compiler is unavailable.",
            )
        try:
            root = installed_root.resolve(strict=True)
            node_modules = (root / "node_modules").resolve(strict=True)
            node_modules.relative_to(root)
        except (OSError, ValueError):
            return self._failure(
                "image-owned TypeScript semantic check",
                "The installed tree has no self-contained node_modules directory.",
            )
        if not node_modules.is_dir():
            return self._failure(
                "image-owned TypeScript semantic check",
                "The installed tree has no node_modules directory.",
            )
        suffix, allow_js = language_settings
        example = work / f"contract_example{suffix}"
        config = work / "apdl-tsconfig.json"
        example.write_text(request.snippet, encoding="utf-8")
        config.write_text(
            json.dumps(
                typescript_config(
                    example_name=example.name,
                    allow_js=allow_js,
                ),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            (work / "node_modules").symlink_to(
                node_modules,
                target_is_directory=True,
            )
        except OSError as exc:
            return self._failure(
                "image-owned TypeScript semantic check",
                f"Could not expose installed declarations read-only: {exc}",
            )
        example.chmod(0o400)
        config.chmod(0o400)
        argv = (
            node.as_posix(),
            compiler.as_posix(),
            "--project",
            config.as_posix(),
            "--pretty",
            "false",
            "--incremental",
            "false",
        )
        try:
            result = self._executor(
                argv,
                cwd=work,
                env=sanitized_environment(home=home),
                timeout_seconds=self._timeout,
                output_limit=self._output_limit,
            )
        except Exception as exc:
            return self._failure(
                " ".join(argv), f"Image-owned TypeScript checker failed: {exc}"
            )
        return self._result(
            result=result,
            command=" ".join(argv),
            tool_version=f"typescript-{TYPESCRIPT_VERSION}",
        )

    def _check_python(
        self,
        request: ContractCheckRequest,
        installed_root: Path,
        work: Path,
        home: Path,
    ) -> ContractCheckResult:
        node = _trusted_root_executable("node")
        if node is None:
            return self._failure(
                "image-owned Pyright semantic check",
                "Image-owned Node is unavailable.",
            )
        checker = _image_node_entrypoint(
            PYRIGHT_PACKAGE,
            PYRIGHT_ENTRYPOINT,
            PYRIGHT_VERSION,
        )
        if checker is None:
            return self._failure(
                "image-owned Pyright semantic check",
                "The exact image-owned Pyright checker is unavailable.",
            )
        site_packages = locate_python_site_packages(installed_root)
        if site_packages is None:
            return self._failure(
                "image-owned Pyright semantic check",
                "The installed tree has no self-contained site-packages directory.",
            )
        example = work / "contract_example.py"
        config = work / "apdl-pyrightconfig.json"
        example.write_text(request.snippet, encoding="utf-8")
        config.write_text(
            json.dumps(
                pyright_config(site_packages=site_packages),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        example.chmod(0o400)
        config.chmod(0o400)
        argv = (
            node.as_posix(),
            checker.as_posix(),
            "--project",
            config.as_posix(),
            "--level",
            "error",
        )
        try:
            result = self._executor(
                argv,
                cwd=work,
                env=sanitized_environment(home=home),
                timeout_seconds=self._timeout,
                output_limit=self._output_limit,
            )
        except Exception as exc:
            return self._failure(
                " ".join(argv), f"Image-owned Pyright checker failed: {exc}"
            )
        return self._result(
            result=result,
            command=" ".join(argv),
            tool_version=f"pyright-{PYRIGHT_VERSION}",
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
