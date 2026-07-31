from __future__ import annotations

import asyncio

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
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_LLM_URL", raising=False)
    monkeypatch.setenv("QUERY_SERVICE_URL", "http://query.test:8082")
    monkeypatch.setenv("CONFIG_SERVICE_URL", "http://config.test:8081")
    monkeypatch.setenv("CODEGEN_SERVICE_URL", "http://codegen.test:8084")
    monkeypatch.setenv("LLM_VAULT_URL", "http://vault.test:8086")
    monkeypatch.setenv("LLM_VAULT_AGENTS_TOKEN", "v" * 32)

    probed_urls = []

    async def fake_probe(client, url, *, headers=None):
        del client, headers
        probed_urls.append(url)
        return "config.test" not in url

    async def fake_codegen_probe(client, *, configured, url):
        del client
        assert configured is True
        probed_urls.append(url)
        return {
            "configured": True,
            "reachable": True,
            "changeset_creation": "tenant_scoped",
        }

    monkeypatch.setattr(readiness, "_probe_endpoint", fake_probe)
    monkeypatch.setattr(readiness, "_probe_codegen_readiness", fake_codegen_probe)

    report = await readiness.capability_report()

    capabilities = report["capabilities"]
    assert report["status"] == "degraded"
    assert capabilities["llm"] == {
        "credential_store": {"configured": True, "operational": True},
        "project_credentials": "tenant_scoped",
    }
    assert capabilities["query"] == {"configured": True, "reachable": True}
    assert capabilities["config"] == {"configured": True, "reachable": False}
    assert capabilities["codegen"] == {
        "configured": True,
        "reachable": True,
        "changeset_creation": "tenant_scoped",
    }
    assert len(probed_urls) == 4
    assert "openai-secret" not in str(report)


@pytest.mark.asyncio
async def test_generic_report_accepts_tenant_scoped_codegen_as_healthy(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_LLM_URL", raising=False)
    monkeypatch.setenv("LLM_VAULT_URL", "http://vault.test:8086")
    monkeypatch.setenv("LLM_VAULT_AGENTS_TOKEN", "v" * 32)

    async def reachable(*_args, **_kwargs):
        return True

    async def tenant_scoped(*_args, **_kwargs):
        return {
            "configured": True,
            "reachable": True,
            "changeset_creation": "tenant_scoped",
        }

    monkeypatch.setattr(readiness, "_probe_endpoint", reachable)
    monkeypatch.setattr(readiness, "_probe_codegen_readiness", tenant_scoped)

    report = await readiness.capability_report()

    assert report["status"] == "available"


@pytest.mark.asyncio
async def test_generic_readiness_never_probes_or_reports_tenant_provider_keys(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_LLM_URL", raising=False)
    monkeypatch.setenv("XAI_API_KEY", "xai-secret")
    monkeypatch.setenv("QUERY_SERVICE_URL", "")
    monkeypatch.setenv("CONFIG_SERVICE_URL", "")
    monkeypatch.setenv("CODEGEN_SERVICE_URL", "")
    monkeypatch.setenv("LLM_VAULT_URL", "")
    monkeypatch.delenv("LLM_VAULT_AGENTS_TOKEN", raising=False)

    probes: list[tuple[str, dict[str, str]]] = []

    async def fake_probe(client, url, *, headers=None):
        del client
        probes.append((url, headers or {}))
        return True

    monkeypatch.setattr(readiness, "_probe_endpoint", fake_probe)

    report = await readiness.capability_report()

    assert probes == []
    assert report["capabilities"]["llm"] == {
        "credential_store": {"configured": False, "operational": False},
        "project_credentials": "tenant_scoped",
    }
    assert "xai-secret" not in str(report)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code, expected", [(200, True), (503, False)])
async def test_endpoint_probe_requires_success_status(status_code, expected) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        reachable = await readiness._probe_endpoint(client, "http://service.test/ready")

    assert reachable is expected


@pytest.mark.asyncio
async def test_codegen_probe_distinguishes_disabled_from_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "status": "ready",
                "service": "apdl-codegen",
                "capabilities": {
                    "changeset_creation": "disabled",
                    "credential_store": "ready",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        disabled = await readiness._probe_codegen_readiness(
            client,
            configured=True,
            url="http://codegen.test/ready",
        )

    assert disabled == {
        "configured": True,
        "reachable": True,
        "changeset_creation": "disabled",
    }

    malformed_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            request=request,
            json={"status": "ready"},
        )
    )
    async with httpx.AsyncClient(transport=malformed_transport) as client:
        unavailable = await readiness._probe_codegen_readiness(
            client,
            configured=True,
            url="http://codegen.test/ready",
        )

    assert unavailable == {
        "configured": True,
        "reachable": False,
        "changeset_creation": "unavailable",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "invalid_state"])
async def test_project_codegen_capability_fails_closed(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readiness, "_PROBE_TIMEOUT_SECONDS", 0.001)

    async def probe(_project_id: str) -> str:
        if failure == "timeout":
            await asyncio.sleep(1)
        return "tenant_scoped"

    monkeypatch.setattr(readiness, "get_changeset_creation_capability", probe)

    assert await readiness.codegen_changeset_capability("demo") == "unavailable"
