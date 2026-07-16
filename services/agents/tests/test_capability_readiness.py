from __future__ import annotations

import httpx
import pytest

from app import readiness


@pytest.mark.asyncio
async def test_capability_report_separates_configuration_and_reachability(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_LLM_URL", raising=False)
    monkeypatch.setenv("QUERY_SERVICE_URL", "http://query.test:8082")
    monkeypatch.setenv("CONFIG_SERVICE_URL", "http://config.test:8081")
    monkeypatch.setenv("CODEGEN_SERVICE_URL", "http://codegen.test:8084")

    probed_urls = []

    async def fake_probe(client, url, *, headers=None):
        del client, headers
        probed_urls.append(url)
        return "config.test" not in url

    monkeypatch.setattr(readiness, "_probe_endpoint", fake_probe)

    report = await readiness.capability_report()

    capabilities = report["capabilities"]
    assert report["status"] == "degraded"
    assert capabilities["llm"]["configured"] is True
    assert capabilities["llm"]["reachable"] is True
    assert capabilities["llm"]["providers"] == {
        "openai": {"configured": True, "reachable": True},
        "anthropic": {"configured": False, "reachable": False},
        "google": {"configured": False, "reachable": False},
        "local": {"configured": False, "reachable": False},
    }
    assert capabilities["query"] == {"configured": True, "reachable": True}
    assert capabilities["config"] == {"configured": True, "reachable": False}
    assert capabilities["codegen"] == {"configured": True, "reachable": True}
    assert len(probed_urls) == 4
    assert "openai-secret" not in str(report)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code, expected", [(200, True), (503, False)])
async def test_endpoint_probe_requires_success_status(status_code, expected) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        reachable = await readiness._probe_endpoint(client, "http://service.test/ready")

    assert reachable is expected
