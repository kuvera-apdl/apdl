#!/usr/bin/env python3
"""Replace one project's LLM policy through a direct operator-only workflow."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.llm.router import ProviderRuntimeConfiguration, provider_runtime_configuration


MAINTENANCE_INHIBITOR_LOCK_ID = 4_158_044_083
MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084
PROJECT_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
PROVIDERS = ("openai", "anthropic", "google", "local")
RESIDENCIES = ("local", "ca", "us", "eu", "global")
DATA_CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
MAX_PRICE_USD_MICROS = 1_000_000_000_000
MAX_BUDGET_USD_MICROS = 1_000_000_000_000_000


@dataclass(frozen=True)
class ProviderPolicyInput:
    provider: str
    model: str
    endpoint_url: str
    data_residency: str
    allowed_data_classifications: tuple[str, ...]
    input_cost_per_million_tokens_usd_micros: int
    output_cost_per_million_tokens_usd_micros: int
    enabled: bool = True


@dataclass(frozen=True)
class PolicyReplacement:
    project_id: str
    required_data_residency: str
    project_daily_cost_limit_usd_micros: int
    run_cost_limit_usd_micros: int
    actor: str
    reason: str
    provider_policies: tuple[ProviderPolicyInput, ...]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replace one authorized project's LLM policy using the Agents "
            "container's exact endpoint and model configuration. Provider "
            "credentials are read only from the container environment."
        )
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--provider", required=True, choices=PROVIDERS)
    parser.add_argument("--data-residency", required=True, choices=RESIDENCIES)
    parser.add_argument(
        "--allowed-data-classifications",
        required=True,
        nargs="+",
        choices=DATA_CLASSIFICATIONS,
    )
    for tier in ("fast", "reasoning"):
        for direction in ("input", "output"):
            parser.add_argument(
                f"--{tier}-{direction}-cost-per-million-tokens-usd-micros",
                required=True,
                type=int,
            )
    parser.add_argument(
        "--project-daily-cost-limit-usd-micros",
        required=True,
        type=int,
    )
    parser.add_argument(
        "--run-cost-limit-usd-micros",
        required=True,
        type=int,
    )
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    return parser.parse_args(argv)


def _single_line(value: str, *, name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise SystemExit(f"{name} is required")
    if len(normalized) > maximum or "\n" in normalized or "\r" in normalized:
        raise SystemExit(
            f"{name} must be a single line of at most {maximum} characters"
        )
    return normalized


def _bounded_integer(
    value: int,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SystemExit(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise SystemExit(f"{name} must be between {minimum} and {maximum}")
    return value


def _pricing(args: argparse.Namespace, tier: str) -> tuple[int, int]:
    values = []
    for direction in ("input", "output"):
        name = f"{tier}_{direction}_cost_per_million_tokens_usd_micros"
        values.append(
            _bounded_integer(
                getattr(args, name),
                name=f"--{name.replace('_', '-')}",
                minimum=0,
                maximum=MAX_PRICE_USD_MICROS,
            )
        )
    return values[0], values[1]


def _provider_policy(
    runtime: ProviderRuntimeConfiguration,
    *,
    model: str,
    residency: str,
    classifications: tuple[str, ...],
    pricing: tuple[int, int],
) -> ProviderPolicyInput:
    return ProviderPolicyInput(
        provider=runtime.provider,
        model=model,
        endpoint_url=runtime.endpoint_url,
        data_residency=residency,
        allowed_data_classifications=classifications,
        input_cost_per_million_tokens_usd_micros=pricing[0],
        output_cost_per_million_tokens_usd_micros=pricing[1],
    )


def validate_replacement(args: argparse.Namespace) -> PolicyReplacement:
    project_id = str(args.project_id).strip()
    if PROJECT_ID.fullmatch(project_id) is None:
        raise SystemExit("--project-id must match ^[A-Za-z0-9]{1,64}$")

    actor = _single_line(args.actor, name="--actor", maximum=512)
    reason = _single_line(args.reason, name="--reason", maximum=2000)
    requested_classifications = tuple(args.allowed_data_classifications)
    if not requested_classifications:
        raise SystemExit("--allowed-data-classifications requires at least one value")
    unknown_classifications = sorted(
        set(requested_classifications) - set(DATA_CLASSIFICATIONS)
    )
    if unknown_classifications:
        raise SystemExit(
            "Unknown data classifications: " + ", ".join(unknown_classifications)
        )
    classifications = tuple(
        item for item in DATA_CLASSIFICATIONS if item in requested_classifications
    )
    if len(classifications) != len(requested_classifications):
        raise SystemExit("--allowed-data-classifications must not contain duplicates")

    provider = str(args.provider)
    if provider not in PROVIDERS:
        raise SystemExit("--provider must be openai, anthropic, google, or local")
    residency = str(args.data_residency)
    if residency not in RESIDENCIES:
        raise SystemExit("--data-residency must be local, ca, us, eu, or global")
    if provider == "local" and residency != "local":
        raise SystemExit("The local provider requires local data residency")
    if provider != "local" and residency == "local":
        raise SystemExit("Remote providers cannot claim local data residency")

    fast_pricing = _pricing(args, "fast")
    reasoning_pricing = _pricing(args, "reasoning")
    if provider == "local" and any((*fast_pricing, *reasoning_pricing)):
        raise SystemExit("The local provider requires zero token prices")
    if provider != "local" and (not any(fast_pricing) or not any(reasoning_pricing)):
        raise SystemExit("Each remote model requires a non-zero input or output price")

    project_budget = _bounded_integer(
        args.project_daily_cost_limit_usd_micros,
        name="--project-daily-cost-limit-usd-micros",
        minimum=1,
        maximum=MAX_BUDGET_USD_MICROS,
    )
    run_budget = _bounded_integer(
        args.run_cost_limit_usd_micros,
        name="--run-cost-limit-usd-micros",
        minimum=1,
        maximum=MAX_BUDGET_USD_MICROS,
    )
    if run_budget > project_budget:
        raise SystemExit("--run-cost-limit-usd-micros cannot exceed the project limit")

    try:
        runtime = provider_runtime_configuration(provider)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if provider != "local" and not runtime.endpoint_url.startswith("https://"):
        raise SystemExit("Remote provider endpoints must use HTTPS")

    by_model: dict[str, ProviderPolicyInput] = {}
    for model, pricing in (
        (runtime.fast_model, fast_pricing),
        (runtime.reasoning_model, reasoning_pricing),
    ):
        policy = _provider_policy(
            runtime,
            model=model,
            residency=residency,
            classifications=classifications,
            pricing=pricing,
        )
        existing = by_model.get(model)
        if existing is not None and existing != policy:
            raise SystemExit(
                "Fast and reasoning prices must match when both tiers use the same model"
            )
        by_model[model] = policy

    return PolicyReplacement(
        project_id=project_id,
        required_data_residency=residency,
        project_daily_cost_limit_usd_micros=project_budget,
        run_cost_limit_usd_micros=run_budget,
        actor=actor,
        reason=reason,
        provider_policies=tuple(by_model[model] for model in sorted(by_model)),
    )


def _row_value(row: Any, name: str) -> Any:
    if isinstance(row, ProviderPolicyInput):
        return getattr(row, name)
    return row[name]


def _endpoint_sha256(endpoint_url: str) -> str:
    return hashlib.sha256(endpoint_url.encode("utf-8")).hexdigest()


def _snapshot(
    *,
    project_policy: Any,
    provider_policies: list[Any] | tuple[ProviderPolicyInput, ...],
) -> dict[str, Any]:
    provider_snapshots = []
    for row in provider_policies:
        classifications = tuple(_row_value(row, "allowed_data_classifications"))
        provider_snapshots.append(
            {
                "provider": str(_row_value(row, "provider")),
                "model": str(_row_value(row, "model")),
                "endpoint_sha256": _endpoint_sha256(
                    str(_row_value(row, "endpoint_url"))
                ),
                "data_residency": str(_row_value(row, "data_residency")),
                "allowed_data_classifications": sorted(classifications),
                "input_cost_per_million_tokens_usd_micros": int(
                    _row_value(row, "input_cost_per_million_tokens_usd_micros")
                ),
                "output_cost_per_million_tokens_usd_micros": int(
                    _row_value(row, "output_cost_per_million_tokens_usd_micros")
                ),
                "enabled": bool(_row_value(row, "enabled")),
            }
        )
    provider_snapshots.sort(key=lambda item: (item["provider"], item["model"]))
    return {
        "schema": "llm_project_policy_snapshot@1",
        "project_policy": {
            "required_data_residency": str(
                _row_value(project_policy, "required_data_residency")
            ),
            "allow_cross_vendor_retry": bool(
                _row_value(project_policy, "allow_cross_vendor_retry")
            ),
            "project_daily_cost_limit_usd_micros": int(
                _row_value(project_policy, "project_daily_cost_limit_usd_micros")
            ),
            "run_cost_limit_usd_micros": int(
                _row_value(project_policy, "run_cost_limit_usd_micros")
            ),
        },
        "provider_policies": provider_snapshots,
    }


def _next_project_policy(replacement: PolicyReplacement) -> dict[str, Any]:
    return {
        "required_data_residency": replacement.required_data_residency,
        "allow_cross_vendor_retry": False,
        "project_daily_cost_limit_usd_micros": (
            replacement.project_daily_cost_limit_usd_micros
        ),
        "run_cost_limit_usd_micros": replacement.run_cost_limit_usd_micros,
    }


async def replace_policy(replacement: PolicyReplacement, *, dsn: str) -> str:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "SELECT pg_advisory_lock_shared($1)",
            MAINTENANCE_INHIBITOR_LOCK_ID,
        )
        await conn.execute(
            "SELECT pg_advisory_lock_shared($1)",
            MAINTENANCE_GUARD_LOCK_ID,
        )
        async with conn.transaction():
            project_policy = await conn.fetchrow(
                """
                SELECT policy.required_data_residency,
                       policy.allow_cross_vendor_retry,
                       policy.project_daily_cost_limit_usd_micros,
                       policy.run_cost_limit_usd_micros,
                       EXISTS (
                           SELECT 1
                           FROM admin_project_execution_authorizations AS authority
                           WHERE authority.project_id = project.project_id
                       ) AS execution_authorized
                FROM admin_projects AS project
                JOIN llm_project_policies AS policy
                  ON policy.project_id = project.project_id
                WHERE project.project_id = $1
                FOR UPDATE OF project, policy
                """,
                replacement.project_id,
            )
            if project_policy is None:
                raise SystemExit(
                    "Project or its migrated LLM policy does not exist; no changes made"
                )
            if not bool(project_policy["execution_authorized"]):
                raise SystemExit(
                    "Project is not authorized for Agents execution; no changes made"
                )

            previous_providers = await conn.fetch(
                """
                SELECT provider, model, endpoint_url, data_residency,
                       allowed_data_classifications,
                       input_cost_per_million_tokens_usd_micros,
                       output_cost_per_million_tokens_usd_micros,
                       enabled
                FROM llm_project_provider_policies
                WHERE project_id = $1
                ORDER BY provider, model
                FOR UPDATE
                """,
                replacement.project_id,
            )
            previous_snapshot = _snapshot(
                project_policy=project_policy,
                provider_policies=list(previous_providers),
            )

            await conn.execute(
                """
                UPDATE llm_project_policies
                SET required_data_residency = $2,
                    allow_cross_vendor_retry = FALSE,
                    project_daily_cost_limit_usd_micros = $3,
                    run_cost_limit_usd_micros = $4,
                    updated_at = now()
                WHERE project_id = $1
                """,
                replacement.project_id,
                replacement.required_data_residency,
                replacement.project_daily_cost_limit_usd_micros,
                replacement.run_cost_limit_usd_micros,
            )
            await conn.execute(
                "DELETE FROM llm_project_provider_policies WHERE project_id = $1",
                replacement.project_id,
            )
            for policy in replacement.provider_policies:
                await conn.execute(
                    """
                    INSERT INTO llm_project_provider_policies (
                        project_id, provider, model, endpoint_url,
                        data_residency, allowed_data_classifications,
                        input_cost_per_million_tokens_usd_micros,
                        output_cost_per_million_tokens_usd_micros, enabled
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE)
                    """,
                    replacement.project_id,
                    policy.provider,
                    policy.model,
                    policy.endpoint_url,
                    policy.data_residency,
                    list(policy.allowed_data_classifications),
                    policy.input_cost_per_million_tokens_usd_micros,
                    policy.output_cost_per_million_tokens_usd_micros,
                )

            next_snapshot = _snapshot(
                project_policy=_next_project_policy(replacement),
                provider_policies=replacement.provider_policies,
            )
            audit_id = await conn.fetchval(
                """
                INSERT INTO llm_project_policy_audit (
                    project_id, actor, reason, previous_policy, next_policy
                )
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
                RETURNING audit_id
                """,
                replacement.project_id,
                replacement.actor,
                replacement.reason,
                json.dumps(previous_snapshot, sort_keys=True, separators=(",", ":")),
                json.dumps(next_snapshot, sort_keys=True, separators=(",", ":")),
            )
            if audit_id is None:
                raise RuntimeError("LLM policy audit insert returned no identity")
            return str(audit_id)
    finally:
        await conn.close()


async def provision(args: argparse.Namespace) -> None:
    replacement = validate_replacement(args)
    dsn = os.getenv("POSTGRES_URL", "").strip()
    if not dsn:
        raise SystemExit("POSTGRES_URL is required")
    audit_id = await replace_policy(replacement, dsn=dsn)
    print(
        f"Provisioned {replacement.provider_policies[0].provider} LLM policy "
        f"for project {replacement.project_id}; audit_id={audit_id}"
    )


if __name__ == "__main__":
    asyncio.run(provision(parse_args(sys.argv[1:])))
