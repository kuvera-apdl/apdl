"""Orchestration for cached, installed-tree dependency contract resolution."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from app.contracts.cache import (
    CacheCorruptionError,
    CacheIdentityError,
    ContractCache,
    build_cache_identity,
)
from app.contracts.inspectors import (
    CheckRunner,
    inspect_node_package,
    inspect_python_package,
)
from app.contracts.models import (
    BlockerCode,
    ContractBlocker,
    ContractBundle,
    ContractInstallRequest,
    ContractInstallResult,
    ContractRequest,
    ContractResolution,
    RuntimeFingerprint,
)


InstallRunner = Callable[[ContractInstallRequest], ContractInstallResult]
EXTRACTOR_VERSION = "phase2-contract-extractor@1"


def _blocking(
    request: ContractRequest, code: BlockerCode, message: str, *paths: str
) -> ContractBlocker:
    return ContractBlocker(
        code=code,
        severity="blocking",
        package_name=request.package_name,
        message=message,
        paths=list(paths),
    )


def _warning(
    request: ContractRequest, code: BlockerCode, message: str, *paths: str
) -> ContractBlocker:
    return ContractBlocker(
        code=code,
        severity="warning",
        package_name=request.package_name,
        message=message,
        paths=list(paths),
    )


def _blocked(
    request: ContractRequest,
    blocker: ContractBlocker,
    *,
    cache_identity=None,
) -> ContractResolution:
    return ContractResolution(
        request=request,
        cache_identity=cache_identity,
        disposition="blocked",
        blockers=[blocker],
    )


def resolve_contract_request(
    repository_root: Path,
    *,
    project_scope: str,
    repository: str,
    request: ContractRequest,
    runtime: RuntimeFingerprint,
    install_runner: InstallRunner | None,
    check_runner: CheckRunner | None = None,
    cache: ContractCache | None = None,
) -> ContractResolution:
    """Resolve one request from an isolated install supplied by the caller.

    The core never chooses a shell command or executes package code itself.  An
    outer sandbox boundary owns installation and returns an installed tree.
    """
    if request.exact_version is None:
        return _blocked(
            request,
            _blocking(
                request,
                BlockerCode.unresolved_version,
                "An exact locked version is required before dependency inspection.",
                request.manifest_path,
            ),
        )
    if request.lockfile_path is None:
        return _blocked(
            request,
            _blocking(
                request,
                BlockerCode.missing_lockfile,
                "No unambiguous lockfile is available for this package boundary.",
                request.manifest_path,
            ),
        )
    try:
        identity = build_cache_identity(
            repository_root,
            project_scope=project_scope,
            repository=repository,
            request=request,
            runtime=runtime,
            extractor_version=EXTRACTOR_VERSION,
        )
    except CacheIdentityError as exc:
        code = (
            BlockerCode.missing_manifest
            if request.manifest_path in str(exc)
            else BlockerCode.missing_lockfile
        )
        return _blocked(
            request,
            _blocking(
                request, code, str(exc), request.manifest_path, request.lockfile_path
            ),
        )

    cache_warning: ContractBlocker | None = None
    if cache is not None:
        try:
            cached = cache.get(identity)
        except CacheCorruptionError:
            cached = None
            cache_warning = _warning(
                request,
                BlockerCode.inspection_failed,
                "A corrupt cached contract was ignored and rebuilt.",
            )
        if cached is not None:
            return cached

    ecosystem = request.ecosystem.casefold()
    if ecosystem not in {"node", "python"}:
        return _blocked(
            request,
            _blocking(
                request,
                BlockerCode.unsupported_ecosystem,
                f"Exact installed-tree inspection is not implemented for {request.ecosystem}.",
            ),
            cache_identity=identity,
        )
    if install_runner is None:
        return _blocked(
            request,
            _blocking(
                request,
                BlockerCode.unsupported_toolchain,
                "No isolated dependency-install runner was supplied.",
            ),
            cache_identity=identity,
        )
    install = install_runner(
        ContractInstallRequest(
            repository_root=repository_root.as_posix(),
            request=request,
            runtime=runtime,
        )
    )
    if install.status != "installed":
        code = (
            BlockerCode.unsupported_toolchain
            if install.status == "unsupported"
            else BlockerCode.install_failed
        )
        return _blocked(
            request,
            _blocking(
                request, code, install.message or "Dependency installation failed."
            ),
            cache_identity=identity,
        )
    installed_root = Path(install.installed_root or "")
    if not installed_root.is_dir():
        return _blocked(
            request,
            _blocking(
                request,
                BlockerCode.install_failed,
                "The install runner returned a missing installed-tree directory.",
                installed_root.as_posix(),
            ),
            cache_identity=identity,
        )

    try:
        if ecosystem == "node":
            evidence, blockers = inspect_node_package(
                installed_root,
                request,
                identity,
                check_runner=check_runner,
            )
        else:
            evidence, blockers = inspect_python_package(
                installed_root,
                request,
                identity,
                check_runner=check_runner,
            )
    except Exception as exc:
        return _blocked(
            request,
            _blocking(
                request,
                BlockerCode.inspection_failed,
                f"Installed dependency inspection failed: {exc}",
            ),
            cache_identity=identity,
        )
    if evidence is None:
        blocking = next(
            (item for item in blockers if item.severity == "blocking"),
            _blocking(
                request,
                BlockerCode.inspection_failed,
                "Installed dependency inspection produced no evidence.",
            ),
        )
        return ContractResolution(
            request=request,
            cache_identity=identity,
            disposition="blocked",
            evidence=None,
            blockers=[blocking, *[item for item in blockers if item is not blocking]],
        )

    resolution = ContractResolution(
        request=request,
        cache_identity=identity,
        disposition="ready",
        evidence=evidence,
        blockers=[*([cache_warning] if cache_warning else []), *blockers],
    )
    if cache is not None:
        cache.put(identity, resolution)
    return resolution


def resolve_contracts(
    repository_root: Path,
    *,
    project_scope: str,
    repository: str,
    requests: Sequence[ContractRequest],
    runtime: RuntimeFingerprint,
    install_runner: InstallRunner | None,
    check_runner: CheckRunner | None = None,
    cache: ContractCache | None = None,
) -> ContractBundle:
    """Resolve a stable ordered bundle of exact package requests."""
    resolutions = [
        resolve_contract_request(
            repository_root,
            project_scope=project_scope,
            repository=repository,
            request=request,
            runtime=runtime,
            install_runner=install_runner,
            check_runner=check_runner,
            cache=cache,
        )
        for request in sorted(
            requests,
            key=lambda item: (
                item.ecosystem,
                item.package_path,
                item.package_name.casefold(),
                item.exact_version or "",
            ),
        )
    ]
    return ContractBundle(resolutions=resolutions)
