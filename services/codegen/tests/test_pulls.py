"""GitHub pull-request creation/observation helpers (no merge capability)."""

import json

import httpx
import pytest

from app.github.pulls import close_pull_request, mark_ready_for_review, open_pull_request


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
                "node_id": "PR_12",
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
    assert pr.node_id == "PR_12"
    assert captured["body"]["draft"] is True
    assert captured["url"].endswith("/repos/acme/widgets/pulls")


@pytest.mark.asyncio
async def test_close_pull_request_only_closes():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"state": "closed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await close_pull_request(
            repo="acme/widgets", number=12, token="ghs_tok", client=client
        )
    assert captured == {"method": "PATCH", "body": {"state": "closed"}}


@pytest.mark.asyncio
async def test_mark_ready_uses_github_graphql():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await mark_ready_for_review(node_id="PR_12", token="ghs_tok", client=client)
    assert captured["body"]["variables"] == {"id": "PR_12"}
