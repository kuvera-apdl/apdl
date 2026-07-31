"""Provider-specific model discovery and normalization contracts."""

from __future__ import annotations

import httpx
import pytest

from app.llm.provider_catalog import (
    CATALOG_VERSION,
    ProviderDiscoveryError,
    discover_models,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "payload", "expected_model", "expected_header"),
    [
        (
            "openai",
            {"data": [{"id": "gpt-5.4-mini"}, {"id": "text-embedding-3-small"}]},
            "gpt-5.4-mini",
            ("authorization", "Bearer provider-secret"),
        ),
        (
            "anthropic",
            {"data": [{"id": "claude-sonnet-4-6"}]},
            "claude-sonnet-4-6",
            ("x-api-key", "provider-secret"),
        ),
        (
            "google",
            {
                "models": [
                    {
                        "name": "models/gemini-2.5-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-2.5-pro",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
            "gemini-2.5-flash",
            ("x-goog-api-key", "provider-secret"),
        ),
        (
            "xai",
            {"data": [{"id": "grok-4.5"}]},
            "grok-4.5",
            ("authorization", "Bearer provider-secret"),
        ),
    ],
)
async def test_discovery_uses_fixed_auth_and_allowlisted_intersection(
    provider: str,
    payload: dict[str, object],
    expected_model: str,
    expected_header: tuple[str, str],
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        models = await discover_models(
            provider,
            "provider-secret",
            client=client,
        )

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.scheme == "https"
    assert requests[0].headers[expected_header[0]] == expected_header[1]
    assert [model.model_id for model in models] == [expected_model]
    assert models[0].provider == provider
    assert models[0].catalog_version == CATALOG_VERSION
    assert models[0].pricing_status == "operator_review_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_status", "code", "api_status"),
    [
        (401, "invalid_key", 401),
        (403, "permission_denied", 403),
        (429, "rate_limited", 429),
        (500, "provider_unavailable", 503),
    ],
)
async def test_discovery_maps_provider_failures_without_retry_or_secret(
    provider_status: int,
    code: str,
    api_status: int,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(provider_status, text="provider-secret diagnostic")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ProviderDiscoveryError) as captured:
            await discover_models(
                "openai",
                "provider-secret",
                client=client,
            )

    assert calls == 1
    assert captured.value.code == code
    assert captured.value.status_code == api_status
    assert "provider-secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_discovery_maps_timeout_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ProviderDiscoveryError) as captured:
            await discover_models("anthropic", "provider-secret", client=client)

    assert calls == 1
    assert captured.value.code == "provider_timeout"
    assert captured.value.status_code == 504
    assert captured.value.__suppress_context__


@pytest.mark.asyncio
async def test_discovery_rejects_malformed_and_unsupported_inventories() -> None:
    responses = iter(
        [
            httpx.Response(200, content=b"not-json"),
            httpx.Response(200, json={"data": [{"id": "embedding-only"}]}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ProviderDiscoveryError) as malformed:
            await discover_models("openai", "secret", client=client)
        with pytest.raises(ProviderDiscoveryError) as unsupported:
            await discover_models("openai", "secret", client=client)

    assert malformed.value.code == "malformed_response"
    assert unsupported.value.code == "no_supported_models"


@pytest.mark.asyncio
async def test_discovery_bounds_provider_response_size() -> None:
    oversized = b"{" + b" " * 1_048_576 + b"}"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ProviderDiscoveryError) as captured:
            await discover_models("openai", "secret", client=client)

    assert captured.value.code == "malformed_response"
    assert "size limit" in str(captured.value)
