"""Bounded provider model discovery used only to validate vault credentials."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal, cast

import httpx

from app.contracts import Provider, PROVIDERS


DiscoveryCode = Literal[
    "invalid_key",
    "permission_denied",
    "rate_limited",
    "provider_timeout",
    "provider_unavailable",
    "malformed_response",
    "no_models",
]


class ProviderDiscoveryError(RuntimeError):
    def __init__(self, code: DiscoveryCode, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


_ENDPOINTS: dict[Provider, str] = {
    "anthropic": "https://api.anthropic.com/v1/models",
    "openai": "https://api.openai.com/v1/models",
    "google": "https://generativelanguage.googleapis.com/v1beta/models",
    "xai": "https://api.x.ai/v1/models",
}
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_MAX_BYTES = 1_048_576
_MAX_MODELS = 1_000


def canonical_provider(provider: str) -> Provider:
    if provider not in PROVIDERS:
        raise ValueError("provider must be anthropic, openai, google, or xai")
    return cast(Provider, provider)


def _headers(provider: Provider, api_key: str) -> dict[str, str]:
    if provider in {"openai", "xai"}:
        return {"Authorization": f"Bearer {api_key}"}
    if provider == "anthropic":
        return {"anthropic-version": "2023-06-01", "x-api-key": api_key}
    return {"x-goog-api-key": api_key}


def _status(response: httpx.Response, provider: Provider) -> None:
    if 200 <= response.status_code < 300:
        return
    if response.status_code == 401 or (
        provider == "google" and response.status_code == 400
    ):
        raise ProviderDiscoveryError(
            "invalid_key", "Provider rejected the credential", 401
        )
    if response.status_code == 403:
        raise ProviderDiscoveryError(
            "permission_denied", "Credential cannot list provider models", 403
        )
    if response.status_code == 429:
        raise ProviderDiscoveryError(
            "rate_limited", "Provider rate limited model discovery", 429
        )
    raise ProviderDiscoveryError(
        "provider_unavailable", "Provider model discovery is unavailable", 503
    )


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _raw_ids(provider: Provider, payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        raise ValueError("provider payload must be an object")
    records = payload.get("models" if provider == "google" else "data")
    if not isinstance(records, list) or len(records) > _MAX_MODELS:
        raise ValueError("provider model list is invalid")
    result: set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("provider model entry is invalid")
        raw = item.get("name" if provider == "google" else "id")
        if not isinstance(raw, str):
            raise ValueError("provider model identifier is invalid")
        if provider == "google":
            methods = item.get("supportedGenerationMethods", [])
            if not isinstance(methods, list):
                raise ValueError("provider model capabilities are invalid")
            if "generateContent" not in methods:
                continue
            raw = raw.removeprefix("models/")
        if not raw or len(raw) > 128:
            raise ValueError("provider model identifier is invalid")
        result.add(raw)
    if not result:
        raise ProviderDiscoveryError("no_models", "Credential exposes no models", 422)
    return tuple(sorted(result))


async def discover_model_ids(provider: str, api_key: str) -> tuple[str, ...]:
    canonical = canonical_provider(provider)
    body = bytearray()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream(
                "GET",
                _ENDPOINTS[canonical],
                headers=_headers(canonical, api_key),
                follow_redirects=False,
            ) as response:
                _status(response, canonical)
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_BYTES:
                        raise ProviderDiscoveryError(
                            "malformed_response",
                            "Provider model response exceeded the size limit",
                            502,
                        )
                    body.extend(chunk)
    except ProviderDiscoveryError:
        raise
    except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
        raise ProviderDiscoveryError(
            "provider_timeout", "Provider model discovery timed out", 504
        ) from exc
    except httpx.HTTPError as exc:
        raise ProviderDiscoveryError(
            "provider_unavailable", "Provider model discovery is unavailable", 503
        ) from exc
    try:
        payload = json.loads(
            body,
            object_pairs_hook=_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        return _raw_ids(canonical, payload)
    except ProviderDiscoveryError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProviderDiscoveryError(
            "malformed_response", "Provider returned an invalid model response", 502
        ) from exc
