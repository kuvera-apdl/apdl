"""Unit test for the GitHub-App PR opener (no network — httpx MockTransport)."""

import json

import httpx
import pytest

from app.github.pulls import open_pull_request


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
