from __future__ import annotations

import asyncio
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


class FailingPool:
    @asynccontextmanager
    async def acquire(self):
        raise ConnectionError("postgres unavailable")
        yield


def expected_payload(
    *,
    status: str = "ready",
    degraded: bool = False,
    core: dict[str, str] | None = None,
    capabilities: dict[str, str] | None = None,
) -> dict:
    default_core = {
        "postgres": "ready",
        "ingestion": "ready",
        "config": "ready",
        "query": "ready",
    }
    default_capabilities = {
        "agents": "ready",
        "codegen": "ready",
    }
    return {
        "status": status,
        "degraded": degraded,
        "core": default_core if core is None else core,
        "capabilities": (
            default_capabilities if capabilities is None else capabilities
        ),
    }


def upstream_response(request: httpx.Request) -> httpx.Response:
    statuses = {
        "http://ingestion.test/health": "ok",
        "http://config.test/ready": "ready",
        "http://query.test/ready": "ready",
        "http://agents.test/ready": "ready",
        "http://codegen.test/ready": "ready",
    }
    return httpx.Response(200, json={"status": statuses[str(request.url)]})


async def readiness_response(
    handler,
    *,
    pool=None,
):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    pg_pool=pool or ReadyPool(),
                    http_client=client,
                    settings=make_settings(),
                )
            )
        )
        return await ready(request)


@pytest.mark.asyncio
async def test_readiness_reports_ready_core_and_capabilities_concurrently() -> None:
    read_timeouts: list[float] = []
    active_probes = 0
    peak_probes = 0

    async def concurrent_upstream(request: httpx.Request) -> httpx.Response:
        nonlocal active_probes, peak_probes
        read_timeouts.append(request.extensions["timeout"]["read"])
        active_probes += 1
        peak_probes = max(peak_probes, active_probes)
        try:
            await asyncio.sleep(0)
            return upstream_response(request)
        finally:
            active_probes -= 1

    response = await readiness_response(concurrent_upstream)

    assert response == expected_payload()
    assert sorted(read_timeouts) == [2.0] * 5
    assert peak_probes > 1


@pytest.mark.asyncio
async def test_optional_capability_failure_is_http_200_degraded() -> None:
    def degraded(request: httpx.Request) -> httpx.Response:
        if request.url.host in {"agents.test", "codegen.test"}:
            return httpx.Response(503, json={"status": "not_ready"})
        return upstream_response(request)

    response = await readiness_response(degraded)

    assert response == expected_payload(
        degraded=True,
        capabilities={
            "agents": "not_ready",
            "codegen": "not_ready",
        },
    )


@pytest.mark.asyncio
async def test_core_failure_is_http_503_even_when_capabilities_are_ready() -> None:
    def unavailable_config(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://config.test/ready":
            return httpx.Response(503, json={"status": "not_ready"})
        return upstream_response(request)

    response = await readiness_response(unavailable_config)

    assert response.status_code == 503
    assert json.loads(response.body) == expected_payload(
        status="not_ready",
        core={
            "postgres": "ready",
            "ingestion": "ready",
            "config": "not_ready",
            "query": "ready",
        },
    )


@pytest.mark.asyncio
async def test_postgres_failure_is_a_core_failure() -> None:
    response = await readiness_response(upstream_response, pool=FailingPool())

    assert response.status_code == 503
    assert json.loads(response.body) == expected_payload(
        status="not_ready",
        core={
            "postgres": "not_ready",
            "ingestion": "ready",
            "config": "ready",
            "query": "ready",
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "response", "expected_status", "degraded"),
    [
        (
            "http://config.test/ready",
            httpx.Response(200, content=b"not-json"),
            "not_ready",
            False,
        ),
        (
            "http://query.test/ready",
            httpx.Response(200, json=[]),
            "not_ready",
            False,
        ),
        (
            "http://agents.test/ready",
            httpx.Response(200, json={"state": "ready"}),
            "not_ready",
            True,
        ),
        (
            "http://codegen.test/ready",
            httpx.Response(200, json={"status": "ok"}),
            "not_ready",
            True,
        ),
    ],
)
async def test_malformed_or_noncanonical_upstream_responses_fail_closed(
    url: str,
    response: httpx.Response,
    expected_status: str,
    degraded: bool,
) -> None:
    def malformed(request: httpx.Request) -> httpx.Response:
        if str(request.url) == url:
            return response
        return upstream_response(request)

    result = await readiness_response(malformed)
    payload = json.loads(result.body) if hasattr(result, "body") else result
    service = httpx.URL(url).host.split(".", 1)[0]
    section = "core" if service in {"config", "query"} else "capabilities"

    assert payload[section][service] == expected_status
    assert payload["degraded"] is degraded
    if service in {"config", "query"}:
        assert result.status_code == 503
        assert payload["status"] == "not_ready"
    else:
        assert payload["status"] == "ready"
