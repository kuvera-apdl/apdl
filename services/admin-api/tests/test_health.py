from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from app.main import ready
from tests.conftest import make_settings


class ReadyConnection:
    async def fetchval(self, query: str) -> int:
        assert query == "SELECT 1"
        return 1


class ReadyPool:
    @asynccontextmanager
    async def acquire(self):
        yield ReadyConnection()


def upstream_response(request: httpx.Request) -> httpx.Response:
    statuses = {
        "http://ingestion.test/health": "ok",
        "http://config.test/health": "ok",
        "http://query.test/ready": "ready",
    }
    return httpx.Response(200, json={"status": statuses[str(request.url)]})


@pytest.mark.asyncio
async def test_readiness_requires_postgres_and_every_core_upstream() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream_response)) as client:
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    pg_pool=ReadyPool(),
                    http_client=client,
                    settings=make_settings(),
                )
            )
        )
        response = await ready(request)

    assert response == {
        "status": "ready",
        "checks": {
            "postgres": "ready",
            "ingestion": "ready",
            "config": "ready",
            "query": "ready",
        },
    }


@pytest.mark.asyncio
async def test_readiness_fails_closed_when_an_upstream_is_not_ready() -> None:
    def degraded(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://query.test/ready":
            return httpx.Response(503, json={"status": "not_ready"})
        return upstream_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(degraded)) as client:
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    pg_pool=ReadyPool(),
                    http_client=client,
                    settings=make_settings(),
                )
            )
        )
        response = await ready(request)

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "status": "not_ready",
        "checks": {
            "postgres": "ready",
            "ingestion": "ready",
            "config": "ready",
            "query": "not_ready",
        },
    }
