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


async def _post(path: str, payload: dict[str, Any], params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.post(path, json=payload, params=params)
        resp.raise_for_status()
        return resp.json()


async def _put(path: str, payload: dict[str, Any], params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.put(path, json=payload, params=params)
        resp.raise_for_status()
        return resp.json()


async def get_active_flags(project_id: str) -> list[dict[str, Any]]:
    """Get all active feature flags for a project.

    Args:
        project_id: The project to query.

    Returns:
        List of flag configurations.
    """
    response = await _get("/v1/admin/flags", params={"project_id": project_id})
    return response.get("flags", []) if isinstance(response, dict) else response


async def create_flag(
    project_id: str,
    key: str,
    name: str,
    description: str = "",
    enabled: bool = False,
    default_value: bool = False,
    rules: list[dict[str, Any]] | None = None,
    fallthrough: dict[str, Any] | None = None,
    evaluation_mode: str = "client",
    auto_disable: bool = True,
    guardrails: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a new feature flag.

    Args:
        project_id: Project to create the flag in.
        key: Unique flag key (e.g. "exp_checkout_redesign").
        name: Human-readable flag name.
        description: Human-readable description of the flag.
        enabled: Whether the flag is initially enabled.
        default_value: Value returned when the flag is disabled or invalid.
        rules: Ordered canonical gate rules.
        fallthrough: Canonical fallthrough config.
        evaluation_mode: One of "client", "server", or "both".
        auto_disable: Whether guardrail automation may disable this gate.
        guardrails: Optional guardrail configs.

    Returns:
        The created flag configuration.
    """
    payload: dict[str, Any] = {
        "key": key,
        "name": name,
        "description": description,
        "enabled": enabled,
        "default_value": default_value,
        "rules": rules or [],
        "fallthrough": fallthrough or {
            "value": False,
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        },
        "evaluation_mode": evaluation_mode,
        "auto_disable": auto_disable,
        "guardrails": guardrails or [],
    }
    return await _post("/v1/admin/flags", payload, params={"project_id": project_id})


async def update_flag(
    project_id: str,
    key: str,
    version: int,
    enabled: bool | None = None,
    name: str | None = None,
    description: str | None = None,
    default_value: bool | None = None,
    rules: list[dict[str, Any]] | None = None,
    fallthrough: dict[str, Any] | None = None,
    evaluation_mode: str | None = None,
    auto_disable: bool | None = None,
    guardrails: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Update an existing feature flag.

    Args:
        project_id: Project containing the flag.
        key: Flag key to update.
        version: Expected current config version.
        enabled: Set flag enabled/disabled state.
        name: Updated name.
        description: Updated description.
        default_value: Updated disabled/invalid fallback.
        rules: Updated canonical gate rules.
        fallthrough: Updated fallthrough config.
        evaluation_mode: Updated evaluation mode.
        auto_disable: Updated guardrail automation setting.
        guardrails: Updated guardrail configs.

    Returns:
        The updated flag configuration.
    """
    payload: dict[str, Any] = {"version": version}
    if enabled is not None:
        payload["enabled"] = enabled
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if default_value is not None:
        payload["default_value"] = default_value
    if rules is not None:
        payload["rules"] = rules
    if fallthrough is not None:
        payload["fallthrough"] = fallthrough
    if evaluation_mode is not None:
        payload["evaluation_mode"] = evaluation_mode
    if auto_disable is not None:
        payload["auto_disable"] = auto_disable
    if guardrails is not None:
        payload["guardrails"] = guardrails
    return await _put(f"/v1/admin/flags/{key}", payload, params={"project_id": project_id})


async def evaluate_gate(
    project_id: str,
    key: str,
    user_id: str = "",
    anonymous_id: str = "",
    attributes: dict[str, Any] | None = None,
    session_id: str = "",
    message_id: str = "",
    page: str = "",
    log_exposure: bool = True,
) -> dict[str, Any]:
    """Evaluate a server-side feature gate through the trusted Config API."""
    internal_token = os.getenv("APDL_INTERNAL_TOKEN", "")
    headers = {"X-APDL-Internal-Token": internal_token} if internal_token else {}
    payload = {
        "project_id": project_id,
        "key": key,
        "context": {
            "user_id": user_id,
            "anonymous_id": anonymous_id,
            "attributes": attributes or {},
        },
        "session_id": session_id,
        "message_id": message_id,
        "page": page,
        "log_exposure": log_exposure,
    }

    async with httpx.AsyncClient(base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.post("/v1/evaluate", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()
