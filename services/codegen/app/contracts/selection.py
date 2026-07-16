"""Deterministic exact-name selection of dependency contract requests."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from app.contracts.models import ContractRequest
from app.profiling.models import Dependency, RepoProfile


_PACKAGE_CHAR = r"A-Za-z0-9@._:/+\-"


def _mentioned_exactly(text: str, package_name: str) -> bool:
    """Match a complete package identifier, never a fuzzy prefix or alias."""
    pattern = rf"(?<![{_PACKAGE_CHAR}]){re.escape(package_name)}(?![{_PACKAGE_CHAR}])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _manifest_for(profile: RepoProfile, dependency: Dependency) -> str | None:
    matches = sorted(
        package.manifest_path
        for package in profile.packages
        if package.path == dependency.package_path
        and package.ecosystem == dependency.ecosystem
    )
    return matches[0] if len(matches) == 1 else None


def _lockfile_for(
    profile: RepoProfile, dependency: Dependency, manifest_path: str
) -> str | None:
    if dependency.source_path in profile.lockfiles:
        return dependency.source_path
    matches = sorted(
        {
            manager.lockfile_path
            for manager in profile.package_managers
            if manager.manifest_path == manifest_path
            and manager.lockfile_path is not None
        }
    )
    return matches[0] if len(matches) == 1 else None


def select_contract_requests(
    profile: RepoProfile,
    text: str,
    *,
    requirement_ids: Sequence[str] = (),
    symbols_by_package: Mapping[str, Sequence[str]] | None = None,
) -> list[ContractRequest]:
    """Select direct dependencies whose complete package name occurs in ``text``.

    Symbol hints are accepted only under the dependency's exact package name;
    this function does not infer aliases or guess symbols from neighboring prose.
    """
    symbols_by_package = symbols_by_package or {}
    requests: list[ContractRequest] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for dependency in sorted(
        profile.dependencies,
        key=lambda item: (
            item.ecosystem,
            item.package_path,
            item.name.casefold(),
            item.resolved_version or "",
        ),
    ):
        if not _mentioned_exactly(text, dependency.name):
            continue
        manifest = _manifest_for(profile, dependency)
        if manifest is None:
            # A request cannot cross a strict pipeline boundary without one
            # unambiguous manifest. The profiler already carries uncertainty.
            continue
        key = (
            dependency.ecosystem,
            dependency.package_path,
            dependency.name,
            dependency.resolved_version,
        )
        if key in seen:
            continue
        seen.add(key)
        symbol_key = next(
            (
                candidate
                for candidate in symbols_by_package
                if candidate.casefold() == dependency.name.casefold()
            ),
            None,
        )
        requests.append(
            ContractRequest(
                requirement_ids=list(requirement_ids),
                ecosystem=dependency.ecosystem,
                package_path=dependency.package_path,
                package_name=dependency.name,
                exact_version=dependency.resolved_version,
                manifest_path=manifest,
                lockfile_path=_lockfile_for(profile, dependency, manifest),
                symbols=list(symbols_by_package.get(symbol_key, ()))
                if symbol_key is not None
                else [],
            )
        )
    return requests
