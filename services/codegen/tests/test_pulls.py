"""Unit test for the GitHub-App PR opener (no network — httpx MockTransport)."""

import json

import httpx
import pytest

from app.github.pulls import mark_ready_for_review, merge_pull_request, open_pull_request


@pytest.mark.asyncio
async def test_open_pull_request_posts_a_draft():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization", "")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={"html_url": "https://github.com/acme/widgets/pull/12", "number": 12},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pr = await open_pull_request(
            repo="acme/widgets",
            head="apdl/add-x-cs_12345678",
            base="main",
            title="Add X",
            body="body",
            token="ghs_tok",
            client=client,
        )

    assert pr.url.endswith("/pull/12")
    assert pr.number == 12
    assert captured["url"].endswith("/repos/acme/widgets/pulls")
    assert captured["auth"] == "Bearer ghs_tok"
    assert captured["body"]["draft"] is True
    assert captured["body"]["head"] == "apdl/add-x-cs_12345678"
    assert captured["body"]["base"] == "main"


@pytest.mark.asyncio
async def test_merge_pull_request_puts_merge():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"merged": True, "sha": "abc123"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await merge_pull_request(
            repo="acme/widgets", number=5, token="ghs_tok", client=client
        )

    assert result.merged is True
    assert result.sha == "abc123"
    assert captured["method"] == "PUT"
    assert captured["url"].endswith("/repos/acme/widgets/pulls/5/merge")
    assert captured["body"]["merge_method"] == "squash"


@pytest.mark.asyncio
async def test_merge_not_mergeable_returns_clean_result_not_raise():
    """A GitHub 405 (not mergeable) is a not-merged result, not an exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(405, json={"message": "Pull Request is not mergeable"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await merge_pull_request(
            repo="acme/widgets", number=5, token="ghs_tok", client=client
        )

    assert result.merged is False
    assert result.reason == "Pull Request is not mergeable"


@pytest.mark.asyncio
async def test_merge_server_error_still_raises():
    """A genuine upstream 5xx must still propagate (not be masked as not-merged)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await merge_pull_request(
                repo="acme/widgets", number=5, token="ghs_tok", client=client
            )


@pytest.mark.asyncio
async def test_mark_ready_for_review_posts_graphql_mutation():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"data": {"markPullRequestReadyForReview": {"pullRequest": {"id": "PR_x"}}}}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await mark_ready_for_review(node_id="PR_x", token="ghs_tok", client=client)

    assert captured["url"].endswith("/graphql")
    assert captured["body"]["variables"]["id"] == "PR_x"
