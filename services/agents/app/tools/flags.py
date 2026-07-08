"""Feature flag management tools — wrappers around the Config Service API."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

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
    state: str | None = None,
    owners: list[str] | None = None,
    review_by: str | None = None,
    enabled: bool | None = None,
    default_variant: str = "control",
    variants: list[dict[str, Any]] | None = None,
    rules: list[dict[str, Any]] | None = None,
    fallthrough: dict[str, Any] | None = None,
    evaluation_mode: str = "client",
    auto_disable: bool = True,
    guardrails: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a new canonical variant feature flag.

    Args:
        project_id: Project to create the flag in.
        key: Unique flag key (e.g. "exp_checkout_redesign").
        name: Human-readable flag name.
        description: Human-readable description of the flag.
        state: Lifecycle state: "draft", "active", or "disabled".
        owners: Users or teams responsible for reviewing the flag.
        review_by: ISO date when the flag should next be reviewed.
        enabled: Whether the flag is initially enabled. Defaults from state when omitted.
        default_variant: Variant returned when the flag is disabled or invalid.
        variants: Canonical variant definitions with "key" and relative integer "weight".
        rules: Ordered canonical variant flag rules.
        fallthrough: Canonical fallthrough config containing only "rollout".
        evaluation_mode: One of "client", "server", or "both".
        auto_disable: Whether guardrail automation may disable this flag.
        guardrails: Optional guardrail configs.

    Returns:
        The created flag configuration.
    """
    resolved_state = state or ("active" if enabled is True else "draft")
    resolved_enabled = enabled if enabled is not None else resolved_state == "active"
    payload: dict[str, Any] = {
        "key": key,
        "name": name,
        "description": description,
        "state": resolved_state,
        "owners": owners or [],
        "review_by": review_by,
        "enabled": resolved_enabled,
        "default_variant": default_variant,
        "variants": variants if variants is not None else [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
        "rules": rules or [],
        "fallthrough": fallthrough if fallthrough is not None else {
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
    state: str | None = None,
    owners: list[str] | None = None,
    review_by: str | None = None,
    enabled: bool | None = None,
    name: str | None = None,
    description: str | None = None,
    default_variant: str | None = None,
    variants: list[dict[str, Any]] | None = None,
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
        state: Updated lifecycle state.
        owners: Updated owner list.
        review_by: Updated ISO review date.
        enabled: Set flag enabled/disabled state.
        name: Updated name.
        description: Updated description.
        default_variant: Updated disabled/invalid fallback variant.
        variants: Updated canonical variant definitions.
        rules: Updated canonical variant flag rules.
        fallthrough: Updated fallthrough config.
        evaluation_mode: Updated evaluation mode.
        auto_disable: Updated guardrail automation setting.
        guardrails: Updated guardrail configs.

    Returns:
        The updated flag configuration.
    """
    payload: dict[str, Any] = {"version": version}
    if state is not None:
        payload["state"] = state
    if owners is not None:
        payload["owners"] = owners
    if review_by is not None:
        payload["review_by"] = review_by
    if enabled is not None:
        payload["enabled"] = enabled
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if default_variant is not None:
        payload["default_variant"] = default_variant
    if variants is not None:
        payload["variants"] = variants
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
    # Flag keys are LLM-authored: quote the path segment so a key containing
    # '/' or '?' cannot reroute the PUT to a different admin endpoint.
    return await _put(f"/v1/admin/flags/{quote(key, safe='')}", payload, params={"project_id": project_id})


async def disable_flag(
    project_id: str,
    key: str,
    reason: str = "experiment_rollback",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Disable a flag through the Config service's canonical rollback path.

    ``source`` is "system": a flag with ``auto_disable`` off makes the config
    service refuse (409) — the flag owner opted out of automated kill switches,
    and the evaluation agent must respect that.
    """
    payload = {"reason": reason, "source": "system", "evidence": evidence or {}}
    return await _post(
        f"/v1/admin/flags/{quote(key, safe='')}/disable",
        payload,
        params={"project_id": project_id},
    )


async def evaluate_gate(
    project_id: str,
    key: str,
    user_id: str = "",
    anonymous_id: str = "",
    attributes: dict[str, Any] | None = None,
    session_id: str = "",
    message_id: str = "",
    page: str = "",
    component: str = "",
    log_exposure: bool = True,
) -> dict[str, Any]:
    """Evaluate a server-side feature flag through the trusted Config API."""
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
        "component": component,
        "log_exposure": log_exposure,
    }

    async with httpx.AsyncClient(base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.post("/v1/evaluate", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()
