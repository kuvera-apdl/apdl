"""Focused security contracts for project LLM provider discovery."""

from __future__ import annotations

import asyncio
import json
import traceback
from typing import Any

import httpx
import pytest

from app.llm import provider_catalog
from app.llm.provider_catalog import (
    CATALOG_VERSION,
    MAX_PROVIDER_MODELS,
    MAX_PROVIDER_RESPONSE_BYTES,
    ProviderDiscoveryError,
    catalog_model,
    discover_models,
    normalize_models,
    runtime_model,
)
from app.store.llm_credentials import PROVIDERS


API_KEY = "provider-secret-that-must-never-appear-in-errors"
PROVIDER_BODY_SECRET = "provider-response-secret-that-must-not-escape"
EXPECTED_CATALOG = {
    "anthropic": {
        "claude-haiku-4-5-20251001",
        "claude-opus-5",
        "claude-sonnet-5",
    },
    "openai": {
        "gpt-4.1",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "o3",
        "o4-mini",
    },
    "google": {
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-pro",
    },
    "xai": {
        "grok-4.20-0309-non-reasoning",
        "grok-4.5",
    },
}
EXPECTED_ENDPOINTS = {
    "anthropic": "https://api.anthropic.com/v1/models",
    "openai": "https://api.openai.com/v1/models",
    "google": "https://generativelanguage.googleapis.com/v1beta/models",
    "xai": "https://api.x.ai/v1/models",
}


def _payload(provider: str, model_ids: list[str]) -> dict[str, object]:
    if provider == "google":
        return {
            "models": [
                {
                    "name": f"models/{model_id}",
                    "supportedGenerationMethods": ["generateContent"],
                }
                for model_id in model_ids
            ]
        }
    return {"data": [{"id": model_id} for model_id in model_ids]}


async def _discovery_error_for_response(
    provider: str,
    *,
    status_code: int = 200,
    content: bytes,
) -> ProviderDiscoveryError:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            content=content,
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        with pytest.raises(ProviderDiscoveryError) as captured:
            await discover_models(provider, API_KEY, client=client)
    return captured.value


def _assert_secret_free(error: BaseException) -> None:
    rendered = "".join(traceback.format_exception(error))
    assert API_KEY not in rendered
    assert PROVIDER_BODY_SECRET not in rendered


def test_provider_and_reviewed_catalog_sets_are_exact() -> None:
    assert PROVIDERS == frozenset(EXPECTED_CATALOG)
    assert set(provider_catalog._CATALOG) == set(EXPECTED_CATALOG)
    assert set(provider_catalog._DISCOVERY_ENDPOINTS) == set(EXPECTED_CATALOG)
    assert set(provider_catalog._RUNTIME_ENDPOINTS) == set(EXPECTED_CATALOG)
    assert set(provider_catalog._LITELLM_PREFIX) == set(EXPECTED_CATALOG)
    assert set(provider_catalog._CREDENTIAL_ENV) == set(EXPECTED_CATALOG)

    for provider, expected_model_ids in EXPECTED_CATALOG.items():
        assert set(provider_catalog._CATALOG[provider]) == expected_model_ids
        for model_id in expected_model_ids:
            model = catalog_model(provider, model_id)
            assert model is not None
            assert model.provider == provider
            assert model.model_id == model_id
            assert model.catalog_version == CATALOG_VERSION
            assert model.schema_version == "codegen_provider_model@1"
            assert model.supported_roles
            assert set(model.supported_roles) <= {"editor", "helper"}
            assert model.context_window_tokens > 0
            assert model.pricing_status == "catalog_reviewed"


def test_normalization_is_the_sorted_catalog_intersection_only() -> None:
    for provider, catalog_ids in EXPECTED_CATALOG.items():
        selected = sorted(catalog_ids)[::2]
        raw_ids = tuple(["unsupported-provider-model", *reversed(selected)])

        normalized = normalize_models(provider, raw_ids)

        assert [model.model_id for model in normalized] == selected
        assert all(model.provider == provider for model in normalized)

    with pytest.raises(ProviderDiscoveryError) as captured:
        normalize_models("openai", ("unsupported-provider-model",))
    assert captured.value.code == "no_supported_models"
    assert captured.value.status_code == 422
    _assert_secret_free(captured.value)


def test_runtime_models_use_fixed_provider_contracts() -> None:
    expected = {
        "anthropic": (
            "anthropic/claude-sonnet-5",
            "ANTHROPIC_API_KEY",
            "https://api.anthropic.com",
        ),
        "openai": (
            "openai/gpt-5.4-mini",
            "OPENAI_API_KEY",
            "https://api.openai.com/v1",
        ),
        "google": (
            "gemini/gemini-2.5-pro",
            "GOOGLE_API_KEY",
            "https://generativelanguage.googleapis.com",
        ),
        "xai": (
            "xai/grok-4.5",
            "XAI_API_KEY",
            "https://api.x.ai/v1",
        ),
    }
    selected = {
        "anthropic": "claude-sonnet-5",
        "openai": "gpt-5.4-mini",
        "google": "gemini-2.5-pro",
        "xai": "grok-4.5",
    }

    for provider, model_id in selected.items():
        model = runtime_model(provider, model_id)
        assert (
            model.litellm_model,
            model.credential_environment_name,
            model.endpoint_url,
        ) == expected[provider]

    with pytest.raises(ValueError, match="reviewed Codegen catalog"):
        runtime_model("openai", "provider-added-but-unreviewed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model_id", "credential_header"),
    [
        ("anthropic", "claude-sonnet-5", "x-api-key"),
        ("openai", "gpt-5.4-mini", "authorization"),
        ("google", "gemini-2.5-pro", "x-goog-api-key"),
        ("xai", "grok-4.5", "authorization"),
    ],
)
async def test_each_adapter_uses_one_fixed_endpoint_and_catalog_intersection(
    provider: str,
    model_id: str,
    credential_header: str,
) -> None:
    requests: list[httpx.Request] = []
    payload = _payload(
        provider,
        ["provider-added-but-unreviewed", model_id],
    )
    if provider == "google":
        models = payload["models"]
        assert isinstance(models, list)
        models.append(
            {
                "name": "models/gemini-ignored-without-generation",
                "supportedGenerationMethods": ["countTokens"],
            }
        )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=payload,
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        models = await discover_models(provider, API_KEY, client=client)

    assert [model.model_id for model in models] == [model_id]
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == EXPECTED_ENDPOINTS[provider]
    assert API_KEY not in str(request.url)
    expected_header_value = (
        f"Bearer {API_KEY}" if credential_header == "authorization" else API_KEY
    )
    assert request.headers[credential_header] == expected_header_value
    if provider == "anthropic":
        assert request.headers["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        b"\xff",
        b"[]",
        b'{"data":[{"id":"gpt-4.1"}],"data":[]}',
        b'{"data":[{"id":"gpt-4.1","id":"o3"}]}',
        b'{"data":[{"id":"gpt-4.1"}],"score":NaN}',
        (b'{"data":[],"diagnostic":"' + PROVIDER_BODY_SECRET.encode() + b'",}'),
    ],
)
async def test_json_adapter_rejects_noncanonical_or_malformed_payloads(
    content: bytes,
) -> None:
    error = await _discovery_error_for_response(
        "openai",
        content=content,
    )

    assert error.code == "malformed_response"
    assert error.status_code == 502
    _assert_secret_free(error)


@pytest.mark.parametrize(
    ("provider", "payload"),
    [
        ("openai", {"data": {}}),
        ("openai", {"data": [1]}),
        ("openai", {"data": [{}]}),
        ("openai", {"data": [{"id": ""}]}),
        (
            "openai",
            {"data": [{"id": "gpt-4.1"}, {"id": "gpt-4.1"}]},
        ),
        (
            "openai",
            {
                "data": [
                    {"id": f"provider-model-{index}"}
                    for index in range(MAX_PROVIDER_MODELS + 1)
                ]
            },
        ),
        (
            "google",
            {
                "models": [
                    {
                        "name": "gemini-2.5-pro",
                        "supportedGenerationMethods": ["generateContent"],
                    }
                ]
            },
        ),
        (
            "google",
            {
                "models": [
                    {
                        "name": "models/gemini-2.5-pro",
                        "supportedGenerationMethods": "generateContent",
                    }
                ]
            },
        ),
        (
            "google",
            {
                "models": [
                    {
                        "name": "models/gemini-2.5-pro",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-2.5-pro",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ]
            },
        ),
    ],
)
def test_inventory_adapters_reject_ambiguous_shapes_and_duplicates(
    provider: str,
    payload: dict[str, object],
) -> None:
    with pytest.raises(ProviderDiscoveryError) as captured:
        provider_catalog._raw_model_ids(provider, payload)

    assert captured.value.code == "malformed_response"
    assert captured.value.status_code == 502
    _assert_secret_free(captured.value)


@pytest.mark.asyncio
async def test_response_size_is_bounded_before_json_parsing() -> None:
    error = await _discovery_error_for_response(
        "openai",
        content=b"x" * (MAX_PROVIDER_RESPONSE_BYTES + 1),
    )

    assert error.code == "malformed_response"
    assert error.status_code == 502
    assert "exceeded the size limit" in str(error)
    _assert_secret_free(error)


@pytest.mark.asyncio
async def test_discovery_never_follows_provider_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) > 1:
            return httpx.Response(
                200,
                json=_payload("openai", ["gpt-4.1"]),
                request=request,
            )
        return httpx.Response(
            302,
            headers={"location": "https://redirect.attacker.invalid/models"},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        with pytest.raises(ProviderDiscoveryError) as captured:
            await discover_models("openai", API_KEY, client=client)

    assert captured.value.code == "provider_unavailable"
    assert captured.value.status_code == 503
    assert [str(request.url) for request in requests] == [EXPECTED_ENDPOINTS["openai"]]
    _assert_secret_free(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "code", "public_status"),
    [
        (401, "invalid_key", 401),
        (403, "permission_denied", 403),
        (429, "rate_limited", 429),
        (500, "provider_unavailable", 503),
    ],
)
async def test_http_status_errors_are_typed_and_secret_free(
    status_code: int,
    code: str,
    public_status: int,
) -> None:
    error = await _discovery_error_for_response(
        "anthropic",
        status_code=status_code,
        content=json.dumps(
            {
                "credential": API_KEY,
                "diagnostic": PROVIDER_BODY_SECRET,
            }
        ).encode(),
    )

    assert error.code == code
    assert error.status_code == public_status
    _assert_secret_free(error)


@pytest.mark.asyncio
async def test_google_bad_request_is_credential_authentication_failure() -> None:
    error = await _discovery_error_for_response(
        "google",
        status_code=400,
        content=json.dumps(
            {
                "credential": API_KEY,
                "diagnostic": PROVIDER_BODY_SECRET,
            }
        ).encode(),
    )

    assert error.code == "invalid_key"
    assert error.status_code == 401
    _assert_secret_free(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_status"),
    [
        ("timeout", "provider_timeout", 504),
        ("network", "provider_unavailable", 503),
    ],
)
async def test_transport_errors_are_mapped_without_leaking_exception_text(
    failure: str,
    expected_code: str,
    expected_status: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        leaked_detail = f"{API_KEY}:{PROVIDER_BODY_SECRET}"
        if failure == "timeout":
            raise httpx.ReadTimeout(leaked_detail, request=request)
        raise httpx.ConnectError(leaked_detail, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ProviderDiscoveryError) as captured:
            await discover_models("xai", API_KEY, client=client)

    assert captured.value.code == expected_code
    assert captured.value.status_code == expected_status
    _assert_secret_free(captured.value)


@pytest.mark.asyncio
async def test_total_deadline_stops_a_dripping_provider_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DrippingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b" "

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=DrippingBody(),
            request=request,
        )

    monkeypatch.setattr(
        provider_catalog,
        "DISCOVERY_TOTAL_TIMEOUT_SECONDS",
        0.025,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ProviderDiscoveryError) as captured:
            await discover_models("openai", API_KEY, client=client)

    assert captured.value.code == "provider_timeout"
    assert captured.value.status_code == 504
    _assert_secret_free(captured.value)


@pytest.mark.asyncio
async def test_owned_client_disables_hostile_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.attacker.invalid:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.attacker.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.attacker.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "")
    real_async_client = httpx.AsyncClient
    captured_options: dict[str, Any] = {}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_payload("openai", ["gpt-4.1"]),
            request=request,
        )

    def capturing_client(**options: Any) -> httpx.AsyncClient:
        captured_options.update(options)
        return real_async_client(
            transport=httpx.MockTransport(handler),
            timeout=options["timeout"],
            follow_redirects=options["follow_redirects"],
            trust_env=options["trust_env"],
        )

    monkeypatch.setattr(provider_catalog.httpx, "AsyncClient", capturing_client)

    models = await discover_models("openai", API_KEY)

    assert [model.model_id for model in models] == ["gpt-4.1"]
    assert captured_options == {
        "timeout": provider_catalog.DISCOVERY_TIMEOUT,
        "follow_redirects": False,
        "trust_env": False,
    }
    assert [str(request.url) for request in requests] == [EXPECTED_ENDPOINTS["openai"]]
