"""Tests for the repo-context document (GitHub-API-backed, no clone)."""

import base64
import json

import httpx
import pytest

from app.github.repo_context import _detect_framework, fetch_repo_context


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _mock_transport(responses: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        for fragment, response in responses.items():
            if fragment in str(request.url):
                return response
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.MockTransport(handler)


def _tree(*paths: str) -> httpx.Response:
    return httpx.Response(
        200, json={"tree": [{"path": p, "type": "blob"} for p in paths]}
    )


@pytest.mark.asyncio
async def test_fetch_repo_context_builds_full_document():
    manifest = {
        "scripts": {"build": "next build", "test": "vitest run"},
        "dependencies": {"next": "^15"},
    }
    transport = _mock_transport(
        {
            "/git/trees/main": _tree("app/page.tsx", "package.json", "lib/utils.ts"),
            "/contents/package.json": httpx.Response(
                200, json={"encoding": "base64", "content": _b64(json.dumps(manifest))}
            ),
            "/readme": httpx.Response(
                200, json={"encoding": "base64", "content": _b64("# Keelstone\nDemo.")}
            ),
        }
    )

    context = await fetch_repo_context(
        repo="acme/widgets",
        branch="main",
        token="ghs_tok",
        client=httpx.AsyncClient(transport=transport),
    )

    assert context["repo"] == "acme/widgets"
    assert context["framework"] == "Next.js (App Router)"
    assert context["scripts"]["build"] == "next build"
    assert context["dependencies"] == ["next"]
    assert context["has_test_script"] is True
    assert "app/page.tsx" in context["paths"]
    assert context["paths_truncated"] is False
    assert context["readme_excerpt"].startswith("# Keelstone")


@pytest.mark.asyncio
async def test_fetch_repo_context_tolerates_missing_manifest_and_readme():
    transport = _mock_transport({"/git/trees/main": _tree("main.go", "go.mod")})

    context = await fetch_repo_context(
        repo="acme/svc",
        branch="main",
        token="ghs_tok",
        client=httpx.AsyncClient(transport=transport),
    )

    assert context["framework"] == "Go"
    assert context["scripts"] == {}
    assert context["readme_excerpt"] == ""


@pytest.mark.asyncio
async def test_fetch_repo_context_excludes_noise_paths():
    transport = _mock_transport(
        {"/git/trees/main": _tree("node_modules/x/index.js", "dist/out.js", "src/a.ts")}
    )

    context = await fetch_repo_context(
        repo="acme/widgets",
        branch="main",
        token="ghs_tok",
        client=httpx.AsyncClient(transport=transport),
    )

    assert context["paths"] == ["src/a.ts"]


def test_detect_framework_variants():
    assert _detect_framework({"next"}, ["pages/index.tsx"]) == "Next.js (Pages Router)"
    assert _detect_framework({"react"}, []) == "React"
    assert _detect_framework(set(), ["manage.py"]) == "Django"
    assert _detect_framework(set(), ["pyproject.toml"]) == "Python"
    assert _detect_framework(set(), ["Cargo.toml"]) == "Rust"
    assert _detect_framework(set(), ["whatever.txt"]) == "unknown"
