"""Capability visibility for the optional Agents operator preview.

Core readiness belongs to :mod:`app.main` and covers only this process and its
PostgreSQL runtime.  This module reports whether optional workflow dependencies
are configured and currently usable without turning them into an orchestration
gate.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

import httpx

from app.tools.code import get_changeset_creation_capability

_PROBE_TIMEOUT_SECONDS = 2.0

CodegenChangesetCapability = Literal["available", "disabled", "unavailable"]


def _endpoint(base_url: str, path: str) -> str:
    base_url = base_url.strip().rstrip("/")
    return f"{base_url}/{path.lstrip('/')}" if base_url else ""


async def _probe_endpoint(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> bool:
    """Return whether an authenticated endpoint accepts a lightweight GET."""
    try:
        async with client.stream("GET", url, headers=headers) as response:
            return response.is_success
    except (httpx.HTTPError, ValueError):
        return False


async def _probe_if_configured(
    client: httpx.AsyncClient,
    *,
    configured: bool,
    url: str,
    headers: dict[str, str] | None = None,
) -> bool:
    if not configured:
        return False
    return await _probe_endpoint(client, url, headers=headers)


async def _probe_codegen_readiness(
    client: httpx.AsyncClient,
    *,
    configured: bool,
    url: str,
) -> dict[str, Any]:
    """Read Codegen's strict readiness/capability contract.

    A reachable process is not enough: offline and shadow deployments are
    healthy but cannot accept the changeset mutations produced by an approval
    command.  Malformed or non-ready responses are unavailable, never inferred
    as publication-capable.
    """
    if not configured:
        return {
            "configured": False,
            "reachable": False,
            "changeset_creation": "disabled",
        }
    try:
        response = await client.get(url)
        body = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return {
            "configured": True,
            "reachable": False,
            "changeset_creation": "unavailable",
        }
    if response.status_code != 200 or not isinstance(body, dict):
        return {
            "configured": True,
            "reachable": False,
            "changeset_creation": "unavailable",
        }
    if set(body) != {"status", "service", "capabilities"}:
        return {
            "configured": True,
            "reachable": False,
            "changeset_creation": "unavailable",
        }
    capabilities = body.get("capabilities")
    if (
        body.get("status") != "ready"
        or body.get("service") != "apdl-codegen"
        or not isinstance(capabilities, dict)
        or set(capabilities) != {"changeset_creation"}
        or capabilities.get("changeset_creation") not in {"tenant_scoped", "disabled"}
    ):
        return {
            "configured": True,
            "reachable": False,
            "changeset_creation": "unavailable",
        }
    return {
        "configured": True,
        "reachable": True,
        "changeset_creation": capabilities["changeset_creation"],
    }


def _provider_probes() -> dict[str, dict[str, Any]]:
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    google_key = os.getenv("GOOGLE_API_KEY", "").strip()
    local_url = os.getenv("LOCAL_LLM_URL", "").strip()

    return {
        "openai": {
            "configured": bool(openai_key),
            "url": _endpoint(
                os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                "models",
            ),
            "headers": {"Authorization": f"Bearer {openai_key}"},
        },
        "anthropic": {
            "configured": bool(anthropic_key),
            "url": _endpoint(
                os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                "v1/models",
            ),
            "headers": {
                "anthropic-version": "2023-06-01",
                "x-api-key": anthropic_key,
            },
        },
        "google": {
            "configured": bool(google_key),
            "url": "https://generativelanguage.googleapis.com/v1beta/models",
            "headers": {"x-goog-api-key": google_key},
        },
        "local": {
            "configured": bool(local_url),
            "url": _endpoint(local_url, "models"),
            "headers": {},
        },
    }


def _service_probes() -> dict[str, dict[str, Any]]:
    service_urls = {
        "query": os.getenv("QUERY_SERVICE_URL", "http://localhost:8082"),
        "config": os.getenv("CONFIG_SERVICE_URL", "http://localhost:8081"),
        "codegen": os.getenv("CODEGEN_SERVICE_URL", "http://localhost:8084"),
    }
    return {
        name: {
            "configured": bool(base_url.strip()),
            "url": _endpoint(base_url, "ready"),
            "headers": {},
        }
        for name, base_url in service_urls.items()
    }


async def capability_report() -> dict[str, Any]:
    """Report optional workflow capabilities without affecting core readiness."""
    provider_probes = _provider_probes()
    service_probes = _service_probes()
    generic_service_probes = {
        name: service_probes[name] for name in ("query", "config")
    }
    generic_probes = {**provider_probes, **generic_service_probes}
    codegen_probe = service_probes["codegen"]

    timeout = httpx.Timeout(_PROBE_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        results = await asyncio.gather(
            *(
                _probe_if_configured(
                    client,
                    configured=probe["configured"],
                    url=probe["url"],
                    headers=probe["headers"],
                )
                for probe in generic_probes.values()
            ),
            _probe_codegen_readiness(
                client,
                configured=codegen_probe["configured"],
                url=codegen_probe["url"],
            ),
        )

    *generic_results, codegen = results
    reachability = dict(zip(generic_probes, generic_results, strict=True))
    providers = {
        name: {
            "configured": probe["configured"],
            "reachable": reachability[name],
        }
        for name, probe in provider_probes.items()
    }
    llm = {
        "configured": any(provider["configured"] for provider in providers.values()),
        "reachable": any(provider["reachable"] for provider in providers.values()),
        "providers": providers,
    }
    services: dict[str, dict[str, Any]] = {
        name: {
            "configured": probe["configured"],
            "reachable": reachability[name],
        }
        for name, probe in generic_service_probes.items()
    }
    services["codegen"] = codegen
    capabilities = {"llm": llm, **services}
    fully_available = (
        all(
            capability["configured"] and capability["reachable"]
            for name, capability in capabilities.items()
            if name != "codegen"
        )
        # Generic Codegen readiness deliberately cannot authorize a tenant.
        # ``tenant_scoped`` means the service is healthy and callers must use
        # its authenticated project capability before mutating.  Requiring the
        # impossible project-only ``available`` state made this report remain
        # degraded even when every generic dependency was healthy.
        and codegen["changeset_creation"] == "tenant_scoped"
    )
    return {
        "status": "available" if fully_available else "degraded",
        "service": "apdl-agents",
        "capabilities": capabilities,
    }


async def codegen_changeset_capability(
    project_id: str,
) -> CodegenChangesetCapability:
    """Return authenticated, executable capability for exactly one project."""
    try:
        capability = await asyncio.wait_for(
            get_changeset_creation_capability(project_id),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (TimeoutError, httpx.HTTPError, RuntimeError, TypeError, ValueError):
        return "unavailable"
    if capability not in {"available", "disabled"}:
        return "unavailable"
    return capability
