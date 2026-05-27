"""Feature flag management tools — wrappers around the Config Service API."""

from __future__ import annotations

import os
from typing import Any

import httpx

CONFIG_SERVICE_URL = os.getenv("CONFIG_SERVICE_URL", "http://localhost:8081")
_TIMEOUT = 15.0


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, payload: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.post(path, json=payload)
        resp.raise_for_status()
        return resp.json()


async def _put(path: str, payload: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.put(path, json=payload)
        resp.raise_for_status()
        return resp.json()


async def get_active_flags(project_id: str) -> list[dict[str, Any]]:
    """Get all active feature flags for a project.

    Args:
        project_id: The project to query.

    Returns:
        List of flag configurations.
    """
    return await _get("/v1/flags", params={"project_id": project_id})


async def create_flag(
    project_id: str,
    key: str,
    description: str,
    variants: list[dict[str, Any]],
    targeting_rules: list[dict[str, Any]] | None = None,
    default_variant: str = "control",
    enabled: bool = False,
) -> dict[str, Any]:
    """Create a new feature flag.

    Args:
        project_id: Project to create the flag in.
        key: Unique flag key (e.g. "exp_checkout_redesign").
        description: Human-readable description of the flag.
        variants: List of variant objects, each with "key" and "weight".
        targeting_rules: Optional targeting rules for the flag.
        default_variant: Variant to serve when targeting rules don't match.
        enabled: Whether the flag is initially enabled.

    Returns:
        The created flag configuration.
    """
    payload: dict[str, Any] = {
        "project_id": project_id,
        "key": key,
        "description": description,
        "variants": variants,
        "default_variant": default_variant,
        "enabled": enabled,
    }
    if targeting_rules:
        payload["targeting_rules"] = targeting_rules
    return await _post("/v1/admin/flags", payload)


async def update_flag(
    key: str,
    enabled: bool | None = None,
    variants: list[dict[str, Any]] | None = None,
    targeting_rules: list[dict[str, Any]] | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Update an existing feature flag.

    Args:
        key: Flag key to update.
        enabled: Set flag enabled/disabled state.
        variants: Updated variant definitions.
        targeting_rules: Updated targeting rules.
        description: Updated description.

    Returns:
        The updated flag configuration.
    """
    payload: dict[str, Any] = {}
    if enabled is not None:
        payload["enabled"] = enabled
    if variants is not None:
        payload["variants"] = variants
    if targeting_rules is not None:
        payload["targeting_rules"] = targeting_rules
    if description is not None:
        payload["description"] = description
    return await _put(f"/v1/admin/flags/{key}", payload)
