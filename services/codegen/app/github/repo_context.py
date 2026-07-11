"""GitHub-backed materialization of the canonical RepoProfile schema."""

from __future__ import annotations

import asyncio
import base64
import logging
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx

from app.config import github_api_url
from app.github.client import gh_client, gh_headers
from app.profiling import RepoProfile, profile_repository
from app.profiling.models import (
    BranchProtection,
    BranchProtectionStatus,
    Uncertainty,
    UncertaintyCode,
)

logger = logging.getLogger(__name__)

_MAX_PATHS = 5000
_MAX_CONTENT_FILES = 250
_EXCLUDE_SEGMENTS = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        "build",
        ".next",
        "vendor",
        "__pycache__",
        ".venv",
        "venv",
        "target",
        "coverage",
    }
)
_CONTENT_NAMES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "pdm.lock",
        "requirements.txt",
        "Pipfile",
        "Pipfile.lock",
        "go.mod",
        "go.sum",
        "Cargo.toml",
        "Cargo.lock",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "gradle.lockfile",
        "packages.lock.json",
        "Makefile",
        "makefile",
        "AGENTS.md",
    }
)


def _safe_paths(tree: list[dict]) -> list[str]:
    paths: list[str] = []
    for entry in tree:
        if entry.get("type") != "blob":
            continue
        raw = str(entry.get("path") or "")
        path = PurePosixPath(raw)
        if (
            not raw
            or path.is_absolute()
            or ".." in path.parts
            or any(segment in _EXCLUDE_SEGMENTS for segment in path.parts)
        ):
            continue
        paths.append(path.as_posix())
    return sorted(set(paths))


def _needs_content(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        name in _CONTENT_NAMES
        or name.startswith("requirements")
        and name.endswith(".txt")
        or path.startswith(".github/workflows/")
        or path.endswith(".go")
        and name == "main.go"
        or path.endswith((".csproj", ".fsproj"))
    )


def _decode_content(payload: dict | None) -> str:
    if not payload or payload.get("encoding") != "base64":
        return ""
    try:
        return base64.b64decode(payload.get("content") or "").decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


async def _fetch_content(
    client: httpx.AsyncClient,
    *,
    api: str,
    repo: str,
    branch: str,
    token: str,
    path: str,
) -> tuple[str, str]:
    response = await client.get(
        f"{api}/repos/{repo}/contents/{quote(path, safe='/')}",
        headers=gh_headers(token),
        params={"ref": branch},
    )
    if response.status_code == 404:
        return path, ""
    response.raise_for_status()
    payload = response.json()
    return path, _decode_content(payload if isinstance(payload, dict) else None)


async def _branch_protection(
    client: httpx.AsyncClient, *, api: str, repo: str, branch: str, token: str
) -> BranchProtection:
    response = await client.get(
        f"{api}/repos/{repo}/branches/{quote(branch, safe='')}",
        headers=gh_headers(token),
    )
    if response.status_code in {403, 404}:
        return BranchProtection()
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("protected"), bool):
        return BranchProtection()
    return BranchProtection(
        status=(
            BranchProtectionStatus.protected
            if payload["protected"]
            else BranchProtectionStatus.unprotected
        ),
        source="github_branch_metadata",
    )


async def fetch_repo_context(
    *,
    repo: str,
    branch: str,
    token: str,
    client: httpx.AsyncClient | None = None,
) -> RepoProfile:
    """Build the canonical profile from a bounded GitHub repository snapshot."""
    api = github_api_url()
    async with gh_client(client) as github:
        tree_response = await github.get(
            f"{api}/repos/{repo}/git/trees/{quote(branch, safe='')}",
            headers=gh_headers(token),
            params={"recursive": "1"},
        )
        tree_response.raise_for_status()
        tree_payload = tree_response.json()
        all_paths = _safe_paths(tree_payload.get("tree", []))
        tree_truncated = (
            bool(tree_payload.get("truncated")) or len(all_paths) > _MAX_PATHS
        )
        snapshot_paths = all_paths[:_MAX_PATHS]
        content_candidates = [path for path in snapshot_paths if _needs_content(path)]
        content_truncated = len(content_candidates) > _MAX_CONTENT_FILES
        content_candidates = content_candidates[:_MAX_CONTENT_FILES]
        protection, contents = await asyncio.gather(
            _branch_protection(github, api=api, repo=repo, branch=branch, token=token),
            asyncio.gather(
                *(
                    _fetch_content(
                        github,
                        api=api,
                        repo=repo,
                        branch=branch,
                        token=token,
                        path=path,
                    )
                    for path in content_candidates
                )
            ),
        )

    with tempfile.TemporaryDirectory(prefix="apdl-profile-") as temp:
        root = Path(temp)
        content_by_path = dict(contents)
        for path in snapshot_paths:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content_by_path.get(path, ""), encoding="utf-8")
        profile = profile_repository(
            root,
            repo=repo,
            branch=branch,
            branch_protection=protection,
            paths_truncated=tree_truncated,
        )
    if content_truncated:
        profile.uncertainties.append(
            Uncertainty(
                code=UncertaintyCode.incomplete_remote_snapshot,
                message=(
                    "Relevant file contents exceeded the remote snapshot budget; "
                    "some manifest or instruction evidence may be incomplete."
                ),
                paths=[],
            )
        )
    return profile
