"""Fixed Codegen provider discovery adapters and reviewed coding-model catalog."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.store.llm_credentials import Provider, canonical_provider


CATALOG_VERSION = "codegen-provider-catalog@1"
MODEL_SCHEMA_VERSION = "codegen_provider_model@1"
MAX_PROVIDER_RESPONSE_BYTES = 1_048_576
MAX_PROVIDER_MODELS = 1_000
DISCOVERY_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
DISCOVERY_TOTAL_TIMEOUT_SECONDS = 15.0

Role = Literal["editor", "helper"]
DiscoveryErrorCode = Literal[
    "invalid_key",
    "permission_denied",
    "rate_limited",
    "provider_timeout",
    "provider_unavailable",
    "malformed_response",
    "no_supported_models",
]


class ProviderDiscoveryError(RuntimeError):
    """Typed provider error that never contains provider material."""

    def __init__(self, code: DiscoveryErrorCode, message: str, *, status_code: int):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ProviderModel:
    schema_version: Literal["codegen_provider_model@1"]
    provider: Provider
    model_id: str
    display_name: str
    supported_roles: tuple[Role, ...]
    catalog_version: str
    context_window_tokens: int
    supports_tool_calling: bool
    supports_structured_output: bool
    data_residency: Literal["ca", "us", "eu", "global"]
    allowed_data_classifications: tuple[
        Literal["public", "internal", "confidential", "restricted"], ...
    ]
    input_cost_per_million_tokens_usd_micros: int
    output_cost_per_million_tokens_usd_micros: int
    pricing_status: Literal["catalog_reviewed"]


@dataclass(frozen=True)
class RuntimeModel:
    provider: Provider
    model_id: str
    litellm_model: str
    credential_environment_name: str
    endpoint_url: str


@dataclass(frozen=True)
class _CatalogEntry:
    display_name: str
    roles: tuple[Role, ...]
    context_window_tokens: int
    input_cost: int
    output_cost: int
    supports_tool_calling: bool = True
    supports_structured_output: bool = True
    residency: Literal["ca", "us", "eu", "global"] = "global"
    classifications: tuple[
        Literal["public", "internal", "confidential", "restricted"], ...
    ] = ("public", "internal", "confidential")


_CATALOG: dict[Provider, dict[str, _CatalogEntry]] = {
    "anthropic": {
        "claude-haiku-4-5-20251001": _CatalogEntry(
            "Claude Haiku 4.5", ("helper",), 200_000, 1_000_000, 5_000_000
        ),
        "claude-sonnet-5": _CatalogEntry(
            "Claude Sonnet 5",
            ("editor", "helper"),
            1_000_000,
            3_000_000,
            15_000_000,
        ),
        "claude-opus-5": _CatalogEntry(
            "Claude Opus 5",
            ("editor", "helper"),
            1_000_000,
            5_000_000,
            25_000_000,
        ),
    },
    "openai": {
        "gpt-5.4-nano": _CatalogEntry(
            "GPT-5.4 Nano", ("helper",), 400_000, 200_000, 1_250_000
        ),
        "gpt-5.4-mini": _CatalogEntry(
            "GPT-5.4 Mini",
            ("editor", "helper"),
            400_000,
            750_000,
            4_500_000,
        ),
        "gpt-4.1": _CatalogEntry(
            "GPT-4.1", ("editor", "helper"), 1_000_000, 2_000_000, 8_000_000
        ),
        "o3": _CatalogEntry(
            "OpenAI o3", ("editor",), 200_000, 2_000_000, 8_000_000
        ),
        "o4-mini": _CatalogEntry(
            "OpenAI o4-mini", ("editor", "helper"), 200_000, 1_100_000, 4_400_000
        ),
    },
    "google": {
        "gemini-2.5-flash-lite": _CatalogEntry(
            "Gemini 2.5 Flash-Lite",
            ("helper",),
            1_048_576,
            100_000,
            400_000,
        ),
        "gemini-2.5-flash": _CatalogEntry(
            "Gemini 2.5 Flash",
            ("editor", "helper"),
            1_048_576,
            300_000,
            2_500_000,
        ),
        "gemini-2.5-pro": _CatalogEntry(
            "Gemini 2.5 Pro",
            ("editor", "helper"),
            1_048_576,
            1_250_000,
            10_000_000,
        ),
    },
    "xai": {
        "grok-4.20-0309-non-reasoning": _CatalogEntry(
            "Grok 4.20 Non-Reasoning",
            ("editor", "helper"),
            1_000_000,
            1_250_000,
            2_500_000,
        ),
        "grok-4.5": _CatalogEntry(
            "Grok 4.5",
            ("editor", "helper"),
            500_000,
            2_000_000,
            6_000_000,
        ),
    },
}

_DISCOVERY_ENDPOINTS: dict[Provider, str] = {
    "anthropic": "https://api.anthropic.com/v1/models",
    "openai": "https://api.openai.com/v1/models",
    "google": "https://generativelanguage.googleapis.com/v1beta/models",
    "xai": "https://api.x.ai/v1/models",
}

_RUNTIME_ENDPOINTS: dict[Provider, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "google": "https://generativelanguage.googleapis.com",
    "xai": "https://api.x.ai/v1",
}

_LITELLM_PREFIX: dict[Provider, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "gemini",
    "xai": "xai",
}

_CREDENTIAL_ENV: dict[Provider, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "xai": "XAI_API_KEY",
}


def catalog_model(provider: str, model_id: str) -> ProviderModel | None:
    canonical = canonical_provider(provider)
    entry = _CATALOG[canonical].get(model_id)
    if entry is None:
        return None
    return ProviderModel(
        schema_version=MODEL_SCHEMA_VERSION,
        provider=canonical,
        model_id=model_id,
        display_name=entry.display_name,
        supported_roles=entry.roles,
        catalog_version=CATALOG_VERSION,
        context_window_tokens=entry.context_window_tokens,
        supports_tool_calling=entry.supports_tool_calling,
        supports_structured_output=entry.supports_structured_output,
        data_residency=entry.residency,
        allowed_data_classifications=entry.classifications,
        input_cost_per_million_tokens_usd_micros=entry.input_cost,
        output_cost_per_million_tokens_usd_micros=entry.output_cost,
        pricing_status="catalog_reviewed",
    )


def runtime_model(provider: str, model_id: str) -> RuntimeModel:
    canonical = canonical_provider(provider)
    if model_id not in _CATALOG[canonical]:
        raise ValueError("model is not present in the reviewed Codegen catalog")
    return RuntimeModel(
        provider=canonical,
        model_id=model_id,
        litellm_model=f"{_LITELLM_PREFIX[canonical]}/{model_id}",
        credential_environment_name=_CREDENTIAL_ENV[canonical],
        endpoint_url=_RUNTIME_ENDPOINTS[canonical],
    )


def _headers(provider: Provider, api_key: str) -> dict[str, str]:
    if provider in {"openai", "xai"}:
        return {"Authorization": f"Bearer {api_key}"}
    if provider == "anthropic":
        return {"anthropic-version": "2023-06-01", "x-api-key": api_key}
    return {"x-goog-api-key": api_key}


def _raise_for_status(response: httpx.Response, provider: Provider) -> None:
    if 200 <= response.status_code < 300:
        return
    if response.status_code == 401 or (
        provider == "google" and response.status_code == 400
    ):
        raise ProviderDiscoveryError(
            "invalid_key", "Provider rejected the credential", status_code=401
        )
    if response.status_code == 403:
        raise ProviderDiscoveryError(
            "permission_denied",
            "Provider credential cannot list models",
            status_code=403,
        )
    if response.status_code == 429:
        raise ProviderDiscoveryError(
            "rate_limited",
            "Provider rate limited model discovery",
            status_code=429,
        )
    raise ProviderDiscoveryError(
        "provider_unavailable",
        "Provider model discovery is unavailable",
        status_code=503,
    )


async def _bounded_json(
    client: httpx.AsyncClient,
    provider: Provider,
    api_key: str,
) -> Any:
    try:
        async with client.stream(
            "GET",
            _DISCOVERY_ENDPOINTS[provider],
            headers=_headers(provider, api_key),
            follow_redirects=False,
        ) as response:
            _raise_for_status(response, provider)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_PROVIDER_RESPONSE_BYTES:
                    raise ProviderDiscoveryError(
                        "malformed_response",
                        "Provider model response exceeded the size limit",
                        status_code=502,
                    )
                body.extend(chunk)
    except ProviderDiscoveryError:
        raise
    except httpx.TimeoutException:
        raise ProviderDiscoveryError(
            "provider_timeout",
            "Provider model discovery timed out",
            status_code=504,
        ) from None
    except httpx.HTTPError:
        raise ProviderDiscoveryError(
            "provider_unavailable",
            "Provider model discovery is unavailable",
            status_code=503,
        ) from None

    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_nonfinite(_value: str) -> None:
        raise ValueError("non-finite value")

    try:
        return json.loads(
            body,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonfinite,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise ProviderDiscoveryError(
            "malformed_response",
            "Provider returned an invalid model response",
            status_code=502,
        ) from None


def _raw_model_ids(provider: Provider, payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        raise ProviderDiscoveryError(
            "malformed_response",
            "Provider returned an invalid model response",
            status_code=502,
        )
    records = payload.get("models" if provider == "google" else "data")
    if not isinstance(records, list) or len(records) > MAX_PROVIDER_MODELS:
        raise ProviderDiscoveryError(
            "malformed_response",
            "Provider returned an invalid model inventory",
            status_code=502,
        )
    identifiers: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise ProviderDiscoveryError(
                "malformed_response",
                "Provider returned an invalid model record",
                status_code=502,
            )
        raw_id = record.get("name" if provider == "google" else "id")
        if not isinstance(raw_id, str) or not raw_id:
            raise ProviderDiscoveryError(
                "malformed_response",
                "Provider returned an invalid model identifier",
                status_code=502,
            )
        if provider == "google":
            if not raw_id.startswith("models/"):
                raise ProviderDiscoveryError(
                    "malformed_response",
                    "Provider returned an invalid model identifier",
                    status_code=502,
                )
            methods = record.get("supportedGenerationMethods", [])
            if not isinstance(methods, list):
                raise ProviderDiscoveryError(
                    "malformed_response",
                    "Provider returned invalid model capabilities",
                    status_code=502,
                )
            if "generateContent" not in methods:
                continue
            raw_id = raw_id.removeprefix("models/")
        identifiers.append(raw_id)
    if len(set(identifiers)) != len(identifiers):
        raise ProviderDiscoveryError(
            "malformed_response",
            "Provider returned duplicate model identifiers",
            status_code=502,
        )
    return tuple(identifiers)


def normalize_models(
    provider: str,
    raw_model_ids: tuple[str, ...],
) -> tuple[ProviderModel, ...]:
    canonical = canonical_provider(provider)
    accessible = set(raw_model_ids)
    result = tuple(
        model
        for model_id in sorted(_CATALOG[canonical])
        if model_id in accessible
        if (model := catalog_model(canonical, model_id)) is not None
    )
    if not result:
        raise ProviderDiscoveryError(
            "no_supported_models",
            "Credential has no APDL-supported coding models",
            status_code=422,
        )
    return result


async def discover_models(
    provider: str,
    api_key: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[ProviderModel, ...]:
    canonical = canonical_provider(provider)
    if not api_key or len(api_key.encode("utf-8")) > 16_384:
        raise ValueError("api_key must contain between 1 and 16384 UTF-8 bytes")
    try:
        async with asyncio.timeout(DISCOVERY_TOTAL_TIMEOUT_SECONDS):
            if client is not None:
                payload = await _bounded_json(client, canonical, api_key)
            else:
                async with httpx.AsyncClient(
                    timeout=DISCOVERY_TIMEOUT,
                    follow_redirects=False,
                    trust_env=False,
                ) as owned:
                    payload = await _bounded_json(owned, canonical, api_key)
    except TimeoutError:
        raise ProviderDiscoveryError(
            "provider_timeout",
            "Provider model discovery timed out",
            status_code=504,
        ) from None
    return normalize_models(canonical, _raw_model_ids(canonical, payload))
