"""GitHub-backed canonical repository profile tests."""

import base64
import json

import httpx
import pytest

from app.github.repo_context import fetch_repo_context
from app.profiling.models import BranchProtectionStatus, UncertaintyCode


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _tree(*paths: str, truncated: bool = False) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "tree": [{"path": path, "type": "blob"} for path in paths],
            "truncated": truncated,
        },
    )


def _transport(
    *,
    tree: httpx.Response,
    contents: dict[str, str] | None = None,
    protected: bool | None = None,
) -> httpx.MockTransport:
    contents = contents or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/git/trees/" in request.url.path:
            return tree
        if "/branches/" in request.url.path:
            if protected is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"protected": protected})
        marker = "/contents/"
        if marker in request.url.path:
            path = request.url.path.split(marker, 1)[1]
            if path in contents:
                return httpx.Response(
                    200,
                    json={"encoding": "base64", "content": _b64(contents[path])},
                )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_fetch_repo_context_returns_strict_node_profile():
    manifest = json.dumps(
        {
            "scripts": {"build": "next build", "test": "vitest run"},
            "dependencies": {"next": "^15"},
            "devDependencies": {"vitest": "^2"},
        }
    )
    lock = json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                "node_modules/next": {"version": "15.1.0"},
                "node_modules/vitest": {"version": "2.1.9"},
            },
        }
    )
    transport = _transport(
        tree=_tree(
            "app/page.tsx",
            "package.json",
            "package-lock.json",
            ".github/workflows/ci.yml",
            "AGENTS.md",
        ),
        contents={
            "package.json": manifest,
            "package-lock.json": lock,
            ".github/workflows/ci.yml": "name: ci",
            "AGENTS.md": "instructions",
        },
        protected=True,
    )

    profile = await fetch_repo_context(
        repo="acme/widgets",
        branch="main",
        token="ghs_tok",
        client=httpx.AsyncClient(transport=transport),
    )

    assert profile.schema_version == "repo_profile@1"
    assert profile.repo == "acme/widgets"
    assert profile.frameworks == ["Next.js"]
    assert (
        next(dep for dep in profile.dependencies if dep.name == "next").resolved_version
        == "15.1.0"
    )
    assert profile.routes[0].path == "app/page.tsx"
    assert profile.ci_workflows[0].path == ".github/workflows/ci.yml"
    assert profile.instructions[0].path == "AGENTS.md"
    assert profile.branch_protection.status is BranchProtectionStatus.protected


@pytest.mark.asyncio
async def test_fetch_repo_context_profiles_go_entrypoint_and_unknown_protection():
    transport = _transport(
        tree=_tree("go.mod", "go.sum", "cmd/api/main.go"),
        contents={
            "go.mod": "module example.com/api\nrequire github.com/go-chi/chi v5.1.0\n",
            "go.sum": "github.com/go-chi/chi v5.1.0 h1:abc\n",
            "cmd/api/main.go": "package main\nfunc main() {}",
        },
    )
    profile = await fetch_repo_context(
        repo="acme/api",
        branch="main",
        token="ghs_tok",
        client=httpx.AsyncClient(transport=transport),
    )
    assert profile.languages == ["Go"]
    assert profile.entrypoints[0].path == "cmd/api/main.go"
    assert profile.branch_protection.status is BranchProtectionStatus.unknown
    assert UncertaintyCode.branch_protection_unknown in {
        uncertainty.code for uncertainty in profile.uncertainties
    }


@pytest.mark.asyncio
async def test_fetch_repo_context_excludes_noise_and_surfaces_truncation():
    transport = _transport(
        tree=_tree(
            "node_modules/pkg/index.js",
            "dist/out.js",
            "src/a.ts",
            truncated=True,
        ),
        protected=False,
    )
    profile = await fetch_repo_context(
        repo="acme/huge",
        branch="main",
        token="ghs_tok",
        client=httpx.AsyncClient(transport=transport),
    )
    assert profile.paths == ["src/a.ts"]
    assert profile.paths_truncated is True
    assert profile.branch_protection.status is BranchProtectionStatus.unprotected
    assert UncertaintyCode.repository_tree_truncated in {
        uncertainty.code for uncertainty in profile.uncertainties
    }
