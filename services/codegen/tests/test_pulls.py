"""GitHub pull-request creation/observation helpers (no merge capability)."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.github.pulls import get_pull_request, open_pull_request
from app.models.observations import GitHubPRStatus


@pytest.mark.asyncio
async def test_open_pull_request_posts_a_draft():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "html_url": "https://github.com/acme/widgets/pull/12",
                "number": 12,
                "draft": True,
                "head": {"sha": "abc123"},
                "updated_at": "2026-07-11T12:00:00Z",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        pr = await open_pull_request(
            repo="acme/widgets",
            head="apdl/add-x",
            base="main",
            title="Add X",
            body="body",
            token="ghs_tok",
            client=client,
        )
    assert pr.number == 12
    assert pr.head_sha == "abc123"
    assert pr.status is GitHubPRStatus.draft
    assert pr.github_updated_at == datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    assert captured["body"]["draft"] is True
    assert captured["url"].endswith("/repos/acme/widgets/pulls")


@pytest.mark.asyncio
async def test_get_pull_request_reads_live_github_state():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "number": 12,
                "state": "open",
                "draft": False,
                "head": {"sha": "abc123"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        data = await get_pull_request(
            "acme/widgets", 12, "ghs_tok", client=client
        )
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/repos/acme/widgets/pulls/12")
    assert data["head"]["sha"] == "abc123"
