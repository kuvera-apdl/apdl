"""Compact repository context for grounding upstream planning agents.

The agents service's feature-proposal step writes the specs codegen later
implements. Written blind, those specs demand infrastructure the connected repo
does not have; this module gives the "brain" a bounded, factual picture of the
repo — its stack, layout, scripts, and README — fetched through the GitHub API
(no clone), so proposals can name real files and stay inside the repo's actual
capabilities. Served by ``GET /v1/connections/{project_id}/repo-context``.
"""

from __future__ import annotations

import base64
import json
import logging

import httpx

from app.config import github_api_url
from app.github.client import gh_client, gh_headers

logger = logging.getLogger(__name__)

#: Path cap: enough for a real app's full shape without shipping a monorepo.
_MAX_PATHS = 400
#: Directory prefixes that never inform a proposal (build output, vendored deps).
_EXCLUDE_SEGMENTS = frozenset(
    {".git", "node_modules", "dist", "build", ".next", "vendor",
     "__pycache__", ".venv", "venv", "target", "coverage"}
)
_README_EXCERPT_CHARS = 2000


def _detect_framework(dependencies: set[str], paths: list[str]) -> str:
    """Human-readable stack label from manifest dependencies + layout."""
    if "next" in dependencies:
        router = "App Router" if any(p.startswith("app/") for p in paths) else "Pages Router"
        return f"Next.js ({router})"
    for dep, label in (
        ("react", "React"), ("vue", "Vue"), ("svelte", "Svelte"), ("angular", "Angular"),
    ):
        if dep in dependencies:
            return label
    if dependencies:
        return "JavaScript/Node"
    if any(p == "manage.py" for p in paths):
        return "Django"
    if any(p in ("pyproject.toml", "requirements.txt", "setup.py") for p in paths):
        return "Python"
    if "go.mod" in paths:
        return "Go"
    if "Cargo.toml" in paths:
        return "Rust"
    return "unknown"


def _filtered_paths(tree: list[dict]) -> tuple[list[str], bool]:
    """File paths from a git tree response, noise excluded, capped."""
    paths: list[str] = []
    truncated = False
    for entry in tree:
        if entry.get("type") != "blob":
            continue
        path = str(entry.get("path") or "")
        if not path or any(seg in _EXCLUDE_SEGMENTS for seg in path.split("/")):
            continue
        if len(paths) >= _MAX_PATHS:
            truncated = True
            break
        paths.append(path)
    return paths, truncated


async def _fetch_json(c: httpx.AsyncClient, url: str, token: str, **params) -> dict | None:
    """GET a GitHub JSON resource; ``None`` on 404 (missing file is not an error)."""
    resp = await c.get(url, headers=gh_headers(token), params=params or None)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else None


def _decode_content(payload: dict | None) -> str:
    """Decode a contents-API payload's base64 body (empty string on any miss)."""
    if not payload or payload.get("encoding") != "base64":
        return ""
    try:
        return base64.b64decode(payload.get("content") or "").decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


async def fetch_repo_context(
    *,
    repo: str,
    branch: str,
    token: str,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Build the repo-context document for ``repo`` at ``branch``.

    One tree listing plus two content fetches (manifest, README) — cheap enough
    to serve per proposal run without caching.
    """
    api = github_api_url()
    async with gh_client(client) as c:
        tree_resp = await c.get(
            f"{api}/repos/{repo}/git/trees/{branch}",
            headers=gh_headers(token),
            params={"recursive": "1"},
        )
        tree_resp.raise_for_status()
        paths, truncated = _filtered_paths(tree_resp.json().get("tree", []))

        scripts: dict = {}
        dependencies: set[str] = set()
        if "package.json" in paths:
            manifest_raw = _decode_content(
                await _fetch_json(
                    c, f"{api}/repos/{repo}/contents/package.json", token, ref=branch
                )
            )
            try:
                manifest = json.loads(manifest_raw) if manifest_raw else {}
            except ValueError:
                manifest = {}
            if isinstance(manifest.get("scripts"), dict):
                scripts = manifest["scripts"]
            for key in ("dependencies", "devDependencies"):
                section = manifest.get(key)
                if isinstance(section, dict):
                    dependencies.update(section)

        readme = _decode_content(
            await _fetch_json(c, f"{api}/repos/{repo}/readme", token, ref=branch)
        )

    return {
        "repo": repo,
        "branch": branch,
        "framework": _detect_framework(dependencies, paths),
        "scripts": scripts,
        "dependencies": sorted(dependencies),
        "paths": paths,
        "paths_truncated": truncated,
        "has_test_script": bool(scripts.get("test")),
        "readme_excerpt": readme[:_README_EXCERPT_CHARS],
    }
