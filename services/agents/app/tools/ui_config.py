"""Server-driven UI configuration management tools — wrappers around the Config Service API."""

from __future__ import annotations

import os
from typing import Any

import httpx

CONFIG_SERVICE_URL = os.getenv("CONFIG_SERVICE_URL", "http://localhost:8081")
_TIMEOUT = 15.0


async def create_ui_config(
    project_id: str,
    config_id: str,
    component: str,
    targeting: dict[str, Any],
    layout: dict[str, Any],
    content: dict[str, Any],
    priority: int = 1,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Create a new server-driven UI configuration.

    Args:
        project_id: Project scope.
        config_id: Unique identifier (e.g. "ui_onboarding_power_users").
        component: UI component type — hero_banner, onboarding_flow,
                   feature_card, notification, recommendation_list.
        targeting: Targeting criteria with "segment" and "conditions".
        layout: Layout specification with "type" and "children".
        content: Content with "title", "body", and optional "cta".
        priority: Display priority (lower = higher priority).
        start_date: Optional ISO date string for scheduling.
        end_date: Optional ISO date string for scheduling.

    Returns:
        The created UI configuration.
    """
    payload: dict[str, Any] = {
        "project_id": project_id,
        "config_id": config_id,
        "component": component,
        "targeting": targeting,
        "layout": layout,
        "content": content,
        "priority": priority,
    }
    if start_date:
        payload["start_date"] = start_date
    if end_date:
        payload["end_date"] = end_date

    async with httpx.AsyncClient(base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.post("/v1/admin/ui-configs", json=payload)
        resp.raise_for_status()
        return resp.json()


async def update_ui_config(
    config_id: str,
    targeting: dict[str, Any] | None = None,
    layout: dict[str, Any] | None = None,
    content: dict[str, Any] | None = None,
    priority: int | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Update an existing server-driven UI configuration.

    Args:
        config_id: Configuration to update.
        targeting: Updated targeting criteria.
        layout: Updated layout specification.
        content: Updated content.
        priority: Updated display priority.
        enabled: Enable or disable the configuration.

    Returns:
        The updated UI configuration.
    """
    payload: dict[str, Any] = {}
    if targeting is not None:
        payload["targeting"] = targeting
    if layout is not None:
        payload["layout"] = layout
    if content is not None:
        payload["content"] = content
    if priority is not None:
        payload["priority"] = priority
    if enabled is not None:
        payload["enabled"] = enabled

    async with httpx.AsyncClient(base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.put(f"/v1/admin/ui-configs/{config_id}", json=payload)
        resp.raise_for_status()
        return resp.json()


async def list_ui_configs(
    project_id: str,
    component: str | None = None,
) -> list[dict[str, Any]]:
    """List server-driven UI configurations for a project.

    Args:
        project_id: Project to query.
        component: Optional filter by component type.

    Returns:
        List of UI configuration objects.
    """
    params: dict[str, Any] = {"project_id": project_id}
    if component:
        params["component"] = component

    async with httpx.AsyncClient(base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.get("/v1/admin/ui-configs", params=params)
        resp.raise_for_status()
        return resp.json()
