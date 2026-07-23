#!/usr/bin/env python3
"""Verify that the pinned Aider wheel is not either affected development build."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, distribution
from pathlib import PurePosixPath
from typing import Iterable

AIDER_DISTRIBUTION = "aider-chat"
AIDER_VERSION = "0.86.2"
FORBIDDEN_DEVELOPMENT_FILES = frozenset({"auth.py", "api_docs.py"})


class AiderDistributionError(RuntimeError):
    """The installed Aider distribution does not match the reviewed wheel."""


def verify_file_manifest(version: str, files: Iterable[str]) -> None:
    """Validate the exact version and absence of advisory-only development files."""
    if version != AIDER_VERSION:
        raise AiderDistributionError(
            f"expected {AIDER_DISTRIBUTION} {AIDER_VERSION}, found {version}"
        )

    paths = tuple(PurePosixPath(value) for value in files)
    if PurePosixPath("aider/__init__.py") not in paths:
        raise AiderDistributionError("installed Aider package manifest is incomplete")

    forbidden = sorted(
        path.as_posix()
        for path in paths
        if path.name in FORBIDDEN_DEVELOPMENT_FILES
    )
    if forbidden:
        raise AiderDistributionError(
            "installed Aider contains advisory development files: "
            + ", ".join(forbidden)
        )


def verify_installed_distribution() -> None:
    """Inspect the installed wheel metadata used by the worker image."""
    try:
        installed = distribution(AIDER_DISTRIBUTION)
    except PackageNotFoundError as exc:
        raise AiderDistributionError("Aider is not installed") from exc
    if installed.files is None:
        raise AiderDistributionError("installed Aider has no file manifest")
    verify_file_manifest(
        installed.version,
        (str(path) for path in installed.files),
    )


if __name__ == "__main__":
    verify_installed_distribution()
    print(f"Verified {AIDER_DISTRIBUTION} {AIDER_VERSION} wheel contents")
