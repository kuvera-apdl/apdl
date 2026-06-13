"""Scheduled frontend-health guardrail monitor."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.clickhouse.client import ClickHouseClient
from app.models.schemas import GuardrailConfig, GuardrailVariantContext
from app.routers.guardrails import evaluate_guardrail

logger = logging.getLogger(__name__)


async def run_guardrail_monitor(
    *,
    ch_client: ClickHouseClient,
    config_service_url: str,
    project_ids: list[str],
    interval_seconds: int,
) -> None:
    """Continuously evaluate configured guardrails and auto-disable failures."""
    while True:
        try:
            await evaluate_projects(
                ch_client=ch_client,
                config_service_url=config_service_url,
                project_ids=project_ids,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Guardrail monitor iteration failed: %s", exc)

        await asyncio.sleep(interval_seconds)


async def evaluate_projects(
    *,
    ch_client: ClickHouseClient,
    config_service_url: str,
    project_ids: list[str],
) -> None:
    async with httpx.AsyncClient(base_url=config_service_url, timeout=15.0) as client:
        for project_id in project_ids:
            await evaluate_project(
                ch_client=ch_client,
                config_client=client,
                project_id=project_id,
            )


async def evaluate_project(
    *,
    ch_client: ClickHouseClient,
    config_client: httpx.AsyncClient,
    project_id: str,
) -> None:
    response = await config_client.get(
        "/v1/admin/flags",
        params={"project_id": project_id},
    )
    response.raise_for_status()

    for flag in response.json().get("flags", []):
        if not _should_monitor(flag):
            continue

        for raw_guardrail in flag.get("guardrails", []):
            try:
                guardrail = GuardrailConfig.model_validate(raw_guardrail)
                variant_context = GuardrailVariantContext.model_validate({
                    "default_variant": flag.get("default_variant"),
                    "variants": flag.get("variants"),
                })
            except ValidationError as exc:
                logger.warning(
                    "Skipping invalid guardrail context for flag %s: %s",
                    flag.get("key", ""),
                    exc,
                )
                continue

            result = await evaluate_guardrail(
                ch_client,
                project_id=project_id,
                flag_key=flag["key"],
                default_variant=variant_context.default_variant,
                variants=variant_context.variants,
                guardrail=guardrail,
            )
            if not result.tripped:
                continue

            await disable_flag(
                config_client=config_client,
                project_id=project_id,
                flag_key=flag["key"],
                evidence=result.evidence,
            )
            break


async def disable_flag(
    *,
    config_client: httpx.AsyncClient,
    project_id: str,
    flag_key: str,
    evidence: dict,
) -> None:
    response = await config_client.post(
        f"/v1/admin/flags/{quote(flag_key, safe='')}/disable",
        params={"project_id": project_id},
        json={
            "reason": "guardrail_failed",
            "source": "system",
            "evidence": evidence,
        },
    )
    response.raise_for_status()
    logger.warning("Auto-disabled flag %s for project %s", flag_key, project_id)


def _should_monitor(flag: dict) -> bool:
    return (
        flag.get("enabled") is True
        and flag.get("auto_disable", True) is True
        and flag.get("archived_at") is None
        and bool(flag.get("guardrails"))
    )
