"""Shared GitHub client pagination boundaries."""

from __future__ import annotations

import httpx
import pytest

from app.github.client import github_paginated_items


@pytest.mark.asyncio
async def test_paginated_items_collects_same_origin_pages_with_auth():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer tok"
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json={"items": [{"id": 2}]})
        return httpx.Response(
            200,
            json={"items": [{"id": 1}]},
            headers={
                "Link": (
                    '<https://api.github.com/resource?page=2>; rel="next"'
                )
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await github_paginated_items(
            client,
            "https://api.github.com/resource?page=1",
            "tok",
            "items",
            max_pages=2,
        )

    assert items == [{"id": 1}, {"id": 2}]
    assert len(requested) == 2


@pytest.mark.asyncio
async def test_paginated_items_rejects_an_off_origin_next_link_before_forwarding_token():
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        return httpx.Response(
            200,
            json={"items": []},
            headers={
                "Link": '<https://attacker.example/resource?page=2>; rel="next"'
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="configured API host"):
            await github_paginated_items(
                client,
                "https://api.github.com/resource?page=1",
                "tok",
                "items",
                max_pages=2,
            )

    assert requested_hosts == ["api.github.com"]


@pytest.mark.asyncio
async def test_paginated_items_rejects_an_off_origin_initial_url_without_a_request():
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"items": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="configured API host"):
            await github_paginated_items(
                client,
                "https://attacker.example/resource?page=1",
                "secret",
                "items",
                max_pages=1,
            )

    assert requests == 0
