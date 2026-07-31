"""Fixed provider model-discovery adapters and normalized model catalog."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, cast

import httpx

from app.store.llm_credentials import REMOTE_PROVIDERS, RemoteProvider


CATALOG_VERSION = "llm-provider-catalog@1"
MODEL_SCHEMA_VERSION = "llm_provider_model@1"
MAX_PROVIDER_RESPONSE_BYTES = 1_048_576
MAX_PROVIDER_MODELS = 1_000
DISCOVERY_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

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
    """A typed, secret-free provider validation or discovery failure."""

    def __init__(
        self,
        code: DiscoveryErrorCode,
        message: str,
        *,
        status_code: int,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ProviderModel:
    schema_version: Literal["llm_provider_model@1"]
    provider: RemoteProvider
    model_id: str
    display_name: str
    supported_tiers: tuple[Literal["fast", "reasoning"], ...]
    catalog_version: str
    data_residency: Literal["ca", "us", "eu", "global"]
    allowed_data_classifications: tuple[
        Literal["public", "internal", "confidential", "restricted"], ...
    ]
    pricing_status: Literal["operator_review_required"]


@dataclass(frozen=True)
class _CatalogEntry:
    display_name: str
    supported_tiers: tuple[Literal["fast", "reasoning"], ...]
    data_residency: Literal["ca", "us", "eu", "global"] = "global"
    allowed_data_classifications: tuple[
        Literal["public", "internal", "confidential", "restricted"], ...
    ] = ("public", "internal", "confidential")


_CATALOG: dict[RemoteProvider, dict[str, _CatalogEntry]] = {
    "openai": {
        "gpt-5.4-nano": _CatalogEntry("GPT-5.4 Nano", ("fast",)),
        "gpt-5.4-mini": _CatalogEntry(
            "GPT-5.4 Mini", ("fast", "reasoning")
        ),
        "gpt-4.1-mini": _CatalogEntry("GPT-4.1 Mini", ("fast",)),
        "o3": _CatalogEntry("OpenAI o3", ("reasoning",)),
        "o4-mini": _CatalogEntry("OpenAI o4-mini", ("reasoning",)),
    },
    "anthropic": {
        "claude-haiku-4-5-20251001": _CatalogEntry(
            "Claude Haiku 4.5", ("fast",)
        ),
        "claude-sonnet-4-6": _CatalogEntry(
            "Claude Sonnet 4.6", ("fast", "reasoning")
        ),
        "claude-opus-4-6": _CatalogEntry("Claude Opus 4.6", ("reasoning",)),
    },
    "google": {
        "gemini-2.5-flash-lite": _CatalogEntry(
            "Gemini 2.5 Flash-Lite", ("fast",)
        ),
        "gemini-2.5-flash": _CatalogEntry(
            "Gemini 2.5 Flash", ("fast", "reasoning")
        ),
        "gemini-2.5-pro": _CatalogEntry("Gemini 2.5 Pro", ("reasoning",)),
    },
    "xai": {
        "grok-4.20-0309-non-reasoning": _CatalogEntry(
            "Grok 4.20 Non-Reasoning", ("fast",)
        ),
        "grok-4.5": _CatalogEntry("Grok 4.5", ("reasoning",)),
    },
}

_ENDPOINTS: dict[RemoteProvider, str] = {
    "openai": "https://api.openai.com/v1/models",
    "anthropic": "https://api.anthropic.com/v1/models",
    "google": "https://generativelanguage.googleapis.com/v1beta/models",
    "xai": "https://api.x.ai/v1/models",
}


def _canonical_provider(provider: str) -> RemoteProvider:
    if provider not in REMOTE_PROVIDERS:
        raise ValueError("provider must be openai, anthropic, google, or xai")
    return cast(RemoteProvider, provider)


def _headers(provider: RemoteProvider, api_key: str) -> dict[str, str]:
    if provider in {"openai", "xai"}:
        return {"Authorization": f"Bearer {api_key}"}
    if provider == "anthropic":
        return {
            "anthropic-version": "2023-06-01",
            "x-api-key": api_key,
        }
    return {"x-goog-api-key": api_key}


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    if response.status_code == 401:
        raise ProviderDiscoveryError(
            "invalid_key",
            "Provider rejected the credential",
            status_code=401,
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
    provider: RemoteProvider,
    api_key: str,
) -> Any:
    try:
        async with client.stream(
            "GET",
            _ENDPOINTS[provider],
            headers=_headers(provider, api_key),
        ) as response:
            _raise_for_status(response)
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
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ProviderDiscoveryError(
            "malformed_response",
            "Provider returned an invalid model response",
            status_code=502,
        ) from None


def _raw_model_ids(provider: RemoteProvider, payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        raise ProviderDiscoveryError(
            "malformed_response",
            "Provider returned an invalid model response",
            status_code=502,
        )
    key = "models" if provider == "google" else "data"
    records = payload.get(key)
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
            raw_id = raw_id.removeprefix("models/")
            methods = record.get("supportedGenerationMethods", [])
            if not isinstance(methods, list) or "generateContent" not in methods:
                continue
        identifiers.append(raw_id)
    return tuple(identifiers)


def normalize_models(
    provider: str,
    raw_model_ids: tuple[str, ...],
) -> tuple[ProviderModel, ...]:
    canonical_provider = _canonical_provider(provider)
    accessible = set(raw_model_ids)
    models = tuple(
        ProviderModel(
            schema_version=MODEL_SCHEMA_VERSION,
            provider=canonical_provider,
            model_id=model_id,
            display_name=entry.display_name,
            supported_tiers=entry.supported_tiers,
            catalog_version=CATALOG_VERSION,
            data_residency=entry.data_residency,
            allowed_data_classifications=entry.allowed_data_classifications,
            pricing_status="operator_review_required",
        )
        for model_id, entry in sorted(_CATALOG[canonical_provider].items())
        if model_id in accessible
    )
    if not models:
        raise ProviderDiscoveryError(
            "no_supported_models",
            "Credential has no APDL-supported models",
            status_code=422,
        )
    return models


async def discover_models(
    provider: str,
    api_key: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[ProviderModel, ...]:
    """Validate one opaque key and return only allowlisted normalized models."""
    canonical_provider = _canonical_provider(provider)
    if not api_key or len(api_key.encode("utf-8")) > 16_384:
        raise ValueError("api_key must contain between 1 and 16384 UTF-8 bytes")
    if client is not None:
        payload = await _bounded_json(client, canonical_provider, api_key)
    else:
        async with httpx.AsyncClient(
            timeout=DISCOVERY_TIMEOUT,
            follow_redirects=False,
        ) as owned_client:
            payload = await _bounded_json(
                owned_client,
                canonical_provider,
                api_key,
            )
    return normalize_models(
        canonical_provider,
        _raw_model_ids(canonical_provider, payload),
    )
