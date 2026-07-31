"""Owner-controlled Agents activation and exact project model assignments."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

from app.llm.provider_catalog import (
    CATALOG_VERSION,
    ProviderModel,
    catalog_model,
    provider_runtime_endpoint,
)
from app.store.llm_credentials import REMOTE_PROVIDERS, RemoteProvider


DEFAULT_PROJECT_DAILY_COST_LIMIT_USD_MICROS = 20_000_000
DEFAULT_RUN_COST_LIMIT_USD_MICROS = 2_000_000

SetupState = Literal["inactive", "active"]
SetupTier = Literal["fast", "reasoning"]
ManagementAuthority = Literal["owner", "delegated", "none"]
SetupBlocker = Literal[
    "project_inactive",
    "fast_model_required",
    "reasoning_model_required",
    "connection_inactive",
    "connection_stale",
    "inventory_stale",
    "model_unavailable",
    "model_ineligible",
    "catalog_stale",
    "credential_unavailable",
    "budget_invalid",
]


class AgentsSetupError(RuntimeError):
    """Base class for strict, secret-free project setup failures."""


class AgentsSetupNotFoundError(AgentsSetupError):
    """The project or its canonical policy row does not exist."""


class AgentsSetupAuthorizationError(AgentsSetupError):
    """The live human actor cannot manage this project's setup."""


class AgentsSetupConflictError(AgentsSetupError):
    """An optimistic setup, connection, or inventory version is stale."""


class AgentsSetupValidationError(AgentsSetupError):
    """A selected provider model is not eligible for the requested tier."""


@dataclass(frozen=True)
class ModelSelection:
    provider: RemoteProvider
    model: str
    connection_version: int
    inventory_version: int


@dataclass(frozen=True)
class SetupAssignment:
    tier: SetupTier
    provider: RemoteProvider
    model: str
    connection_version: int
    inventory_version: int
    model_catalog_version: str
    display_name: str
    endpoint_url: str
    endpoint_host: str
    data_residency: str
    allowed_data_classifications: tuple[str, ...]
    input_cost_per_million_tokens_usd_micros: int
    output_cost_per_million_tokens_usd_micros: int
    current: bool
    assigned_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SetupConnection:
    provider: RemoteProvider
    connection_version: int
    inventory_version: int
    state: Literal["active", "revoked"]
    catalog_version: str
    current: bool
    validated_at: datetime


@dataclass(frozen=True)
class AgentsProjectSetup:
    project_id: str
    state: SetupState
    version: int
    management_authority: ManagementAuthority
    can_manage: bool
    assignments: tuple[SetupAssignment, ...]
    connections: tuple[SetupConnection, ...]
    blockers: tuple[SetupBlocker, ...]
    analysis_ready: bool
    required_data_residency: str
    allow_cross_vendor_retry: bool
    project_daily_cost_limit_usd_micros: int
    run_cost_limit_usd_micros: int
    effectful_execution_authorized: bool
    effectful_execution_authorization_source: str | None
    activated_at: datetime | None
    deactivated_at: datetime | None
    deactivation_reason: str | None


def _provider(value: str) -> RemoteProvider:
    if value not in REMOTE_PROVIDERS:
        raise AgentsSetupValidationError(
            "provider must be openai, anthropic, google, or xai"
        )
    return cast(RemoteProvider, value)


def _snapshot(
    *,
    state: str,
    version: int,
    project_budget: int,
    run_budget: int,
    assignments: list[Any] | tuple[tuple[SetupTier, ModelSelection], ...],
) -> dict[str, Any]:
    normalized_assignments: list[dict[str, Any]] = []
    for item in assignments:
        if isinstance(item, tuple):
            tier, selection = item
            normalized_assignments.append(
                {
                    "tier": tier,
                    "provider": selection.provider,
                    "model": selection.model,
                    "connection_version": selection.connection_version,
                    "inventory_version": selection.inventory_version,
                    "model_catalog_version": CATALOG_VERSION,
                }
            )
        else:
            normalized_assignments.append(
                {
                    "tier": str(item["tier"]),
                    "provider": str(item["provider"]),
                    "model": str(item["model"]),
                    "model_catalog_version": str(
                        item["model_catalog_version"]
                    ),
                    "connection_version": int(item["connection_version"]),
                    "inventory_version": int(item["inventory_version"]),
                }
            )
    normalized_assignments.sort(key=lambda value: value["tier"])
    return {
        "schema_version": "agents_project_setup_snapshot@1",
        "state": state,
        "version": version,
        "project_daily_cost_limit_usd_micros": project_budget,
        "run_cost_limit_usd_micros": run_budget,
        "assignments": normalized_assignments,
    }


class AgentsSetupStore:
    """Serialize setup mutations and derive readiness from current authority."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @staticmethod
    async def _management_authority(
        conn: Any,
        project_id: str,
        actor_user_id: UUID | None,
        *,
        lock: bool,
    ) -> ManagementAuthority:
        if actor_user_id is None:
            return "none"
        suffix = "FOR UPDATE OF project, account" if lock else ""
        row = await conn.fetchrow(
            f"""
            SELECT project.owner_user_id, account.active
            FROM admin_projects AS project
            JOIN admin_users AS account ON account.user_id = $2
            WHERE project.project_id = $1
            {suffix}
            """,
            project_id,
            actor_user_id,
        )
        if row is None or not bool(row["active"]):
            return "none"
        if row["owner_user_id"] == actor_user_id:
            return "owner"
        role_lock = "FOR SHARE" if lock else ""
        roles = await conn.fetchval(
            f"""
            SELECT roles
            FROM admin_user_projects
            WHERE project_id = $1 AND user_id = $2
            {role_lock}
            """,
            project_id,
            actor_user_id,
        )
        effective_roles = {str(role) for role in (roles or [])}
        if {"agents:manage", "credentials:manage"} <= effective_roles:
            return "delegated"
        return "none"

    @staticmethod
    async def _policy(conn: Any, project_id: str, *, lock: bool) -> Any:
        suffix = "FOR UPDATE OF policy" if lock else ""
        return await conn.fetchrow(
            f"""
            SELECT policy.*,
                   execution.authorization_source
                       AS effectful_execution_authorization_source
            FROM llm_project_policies AS policy
            LEFT JOIN admin_project_execution_authorizations AS execution
              ON execution.project_id = policy.project_id
            WHERE policy.project_id = $1
            {suffix}
            """,
            project_id,
        )

    @staticmethod
    async def _assignment_rows(conn: Any, project_id: str) -> list[Any]:
        return list(
            await conn.fetch(
                """
                SELECT assignment.tier, assignment.provider,
                       assignment.model,
                       assignment.model_catalog_version,
                       assignment.assigned_at, assignment.updated_at,
                       connection.version AS connection_version,
                       connection.inventory_version,
                       provider_policy.endpoint_url AS policy_endpoint_url,
                       provider_policy.data_residency
                           AS policy_data_residency,
                       provider_policy.allowed_data_classifications
                           AS policy_allowed_data_classifications,
                       provider_policy.input_cost_per_million_tokens_usd_micros
                           AS policy_input_cost,
                       provider_policy.output_cost_per_million_tokens_usd_micros
                           AS policy_output_cost
                FROM llm_project_model_assignments AS assignment
                JOIN llm_project_provider_connections AS connection
                  ON connection.project_id = assignment.project_id
                 AND connection.provider = assignment.provider
                LEFT JOIN llm_project_provider_policies AS provider_policy
                  ON provider_policy.project_id = assignment.project_id
                 AND provider_policy.provider = assignment.provider
                 AND provider_policy.model = assignment.model
                WHERE assignment.project_id = $1
                ORDER BY assignment.tier
                """,
                project_id,
            )
        )

    async def get(
        self,
        project_id: str,
        *,
        actor_user_id: UUID | None,
    ) -> AgentsProjectSetup:
        async with self._pool.acquire() as conn:
            async with conn.transaction(
                isolation="repeatable_read",
                readonly=True,
            ):
                policy = await self._policy(conn, project_id, lock=False)
                if policy is None:
                    raise AgentsSetupNotFoundError(
                        "Agents project setup was not found"
                    )
                authority = await self._management_authority(
                    conn,
                    project_id,
                    actor_user_id,
                    lock=False,
                )
                assignment_rows = await self._assignment_rows(conn, project_id)
                connection_rows = list(
                    await conn.fetch(
                        """
                        SELECT connection.*,
                               EXISTS (
                                   SELECT 1
                                   FROM llm_vault_provider_credentials
                                       AS credential
                                   JOIN llm_vault_connection_consumers
                                       AS consumer
                                     ON consumer.connection_id =
                                        credential.connection_id
                                    AND consumer.consumer = 'agents'
                                   WHERE credential.credential_id =
                                         connection.credential_id
                                     AND credential.project_id =
                                         connection.project_id
                                     AND credential.provider =
                                         connection.provider
                                     AND credential.state = 'active'
                               ) AS credential_active
                        FROM llm_project_provider_connections AS connection
                        WHERE connection.project_id = $1
                        ORDER BY connection.provider
                        """,
                        project_id,
                    )
                )
                model_rows = list(
                    await conn.fetch(
                        """
                        SELECT model.*
                        FROM llm_project_provider_models AS model
                        JOIN llm_project_provider_connections AS connection
                          ON connection.project_id = model.project_id
                         AND connection.provider = model.provider
                         AND connection.version = model.connection_version
                         AND connection.inventory_version =
                             model.inventory_version
                        WHERE model.project_id = $1
                        """,
                        project_id,
                    )
                )

        connections_by_provider = {
            str(row["provider"]): row for row in connection_rows
        }
        current_models = {
            (str(row["provider"]), str(row["model_id"])): row
            for row in model_rows
        }
        blockers: set[SetupBlocker] = set()
        assignments: list[SetupAssignment] = []
        assignment_tiers = {str(row["tier"]) for row in assignment_rows}
        if "fast" not in assignment_tiers:
            blockers.add("fast_model_required")
        if "reasoning" not in assignment_tiers:
            blockers.add("reasoning_model_required")

        for row in assignment_rows:
            tier = cast(SetupTier, str(row["tier"]))
            provider = _provider(str(row["provider"]))
            model_id = str(row["model"])
            connection = connections_by_provider.get(provider)
            model_row = current_models.get((provider, model_id))
            reviewed = catalog_model(provider, model_id)
            assignment_current = True
            if connection is None:
                blockers.add("connection_inactive")
                continue
            if str(connection["state"]) != "active":
                blockers.add("connection_inactive")
                assignment_current = False
            if not bool(connection["credential_active"]):
                blockers.add("credential_unavailable")
                assignment_current = False
            if str(connection["catalog_version"]) != CATALOG_VERSION:
                blockers.add("connection_stale")
                assignment_current = False
            if model_row is None:
                blockers.add("model_unavailable")
                assignment_current = False
            if reviewed is None:
                blockers.add("catalog_stale")
                assignment_current = False
            if model_row is not None and (
                str(model_row["catalog_version"]) != CATALOG_VERSION
                or str(row["model_catalog_version"]) != CATALOG_VERSION
            ):
                blockers.add("catalog_stale")
                assignment_current = False
            if model_row is not None and tier not in {
                str(value) for value in model_row["supported_tiers"]
            }:
                blockers.add("model_ineligible")
                assignment_current = False
            if model_row is not None and (
                int(model_row["connection_version"])
                != int(connection["version"])
                or int(model_row["inventory_version"])
                != int(connection["inventory_version"])
            ):
                blockers.add("inventory_stale")
                assignment_current = False
            endpoint_url = (
                str(row.get("policy_endpoint_url"))
                if row.get("policy_endpoint_url") is not None
                else provider_runtime_endpoint(provider)
            )
            endpoint_host = urlsplit(endpoint_url).hostname
            if endpoint_host is None:
                blockers.add("catalog_stale")
                assignment_current = False
                endpoint_host = (
                    reviewed.endpoint_host if reviewed is not None else provider
                )
            display_name = (
                str(model_row["display_name"])
                if model_row is not None
                else (
                    reviewed.display_name
                    if reviewed is not None
                    else model_id
                )
            )
            data_residency = (
                str(row.get("policy_data_residency"))
                if row.get("policy_data_residency") is not None
                else (
                    str(model_row["data_residency"])
                    if model_row is not None
                    else (
                        reviewed.data_residency
                        if reviewed is not None
                        else "global"
                    )
                )
            )
            allowed_data_classifications = tuple(
                str(value)
                for value in (
                    row.get("policy_allowed_data_classifications")
                    or (
                        model_row["allowed_data_classifications"]
                        if model_row is not None
                        else (
                            reviewed.allowed_data_classifications
                            if reviewed is not None
                            else ("public",)
                        )
                    )
                )
            )
            assignments.append(
                SetupAssignment(
                    tier=tier,
                    provider=provider,
                    model=model_id,
                    connection_version=int(connection["version"]),
                    inventory_version=int(connection["inventory_version"]),
                    model_catalog_version=str(
                        row["model_catalog_version"]
                    ),
                    display_name=display_name,
                    endpoint_url=endpoint_url,
                    endpoint_host=endpoint_host,
                    data_residency=data_residency,
                    allowed_data_classifications=allowed_data_classifications,
                    input_cost_per_million_tokens_usd_micros=(
                        int(row.get("policy_input_cost"))
                        if row.get("policy_input_cost") is not None
                        else (
                            reviewed.input_cost_per_million_tokens_usd_micros
                            if reviewed is not None
                            else 0
                        )
                    ),
                    output_cost_per_million_tokens_usd_micros=(
                        int(row.get("policy_output_cost"))
                        if row.get("policy_output_cost") is not None
                        else (
                            reviewed.output_cost_per_million_tokens_usd_micros
                            if reviewed is not None
                            else 0
                        )
                    ),
                    current=assignment_current,
                    assigned_at=row["assigned_at"],
                    updated_at=row["updated_at"],
                )
            )

        project_budget = int(
            policy["project_daily_cost_limit_usd_micros"]
        )
        run_budget = int(policy["run_cost_limit_usd_micros"])
        if (
            project_budget <= 0
            or run_budget <= 0
            or run_budget > project_budget
        ):
            blockers.add("budget_invalid")
        state = cast(SetupState, str(policy["state"]))
        if state == "inactive":
            blockers.add("project_inactive")
        ordered_blockers = tuple(sorted(blockers))
        source = policy["effectful_execution_authorization_source"]
        connections = tuple(
            SetupConnection(
                provider=_provider(str(row["provider"])),
                connection_version=int(row["version"]),
                inventory_version=int(row["inventory_version"]),
                state=cast(
                    Literal["active", "revoked"],
                    str(row["state"]),
                ),
                catalog_version=str(row["catalog_version"]),
                current=(
                    str(row["state"]) == "active"
                    and bool(row["credential_active"])
                    and str(row["catalog_version"]) == CATALOG_VERSION
                ),
                validated_at=row["validated_at"],
            )
            for row in connection_rows
        )
        return AgentsProjectSetup(
            project_id=project_id,
            state=state,
            version=int(policy["version"]),
            management_authority=authority,
            can_manage=authority != "none",
            assignments=tuple(sorted(assignments, key=lambda item: item.tier)),
            connections=connections,
            blockers=ordered_blockers,
            analysis_ready=state == "active" and not ordered_blockers,
            required_data_residency=str(policy["required_data_residency"]),
            allow_cross_vendor_retry=bool(
                policy["allow_cross_vendor_retry"]
            ),
            project_daily_cost_limit_usd_micros=project_budget,
            run_cost_limit_usd_micros=run_budget,
            effectful_execution_authorized=source is not None,
            effectful_execution_authorization_source=(
                str(source) if source is not None else None
            ),
            activated_at=policy["activated_at"],
            deactivated_at=policy["deactivated_at"],
            deactivation_reason=policy["deactivation_reason"],
        )

    @staticmethod
    async def _validate_selection(
        conn: Any,
        *,
        project_id: str,
        tier: SetupTier,
        selection: ModelSelection,
    ) -> ProviderModel:
        row = await conn.fetchrow(
            """
            SELECT model.supported_tiers,
                   model.catalog_version AS model_catalog_version,
                   connection.catalog_version AS connection_catalog_version,
                   credential.state AS credential_state
            FROM llm_project_provider_connections AS connection
            JOIN llm_project_provider_models AS model
              ON model.project_id = connection.project_id
             AND model.provider = connection.provider
             AND model.connection_version = connection.version
             AND model.inventory_version = connection.inventory_version
            JOIN llm_vault_provider_credentials AS credential
              ON credential.credential_id = connection.credential_id
             AND credential.project_id = connection.project_id
             AND credential.provider = connection.provider
            JOIN llm_vault_connection_consumers AS consumer
              ON consumer.connection_id = credential.connection_id
             AND consumer.project_id = credential.project_id
             AND consumer.provider = credential.provider
             AND consumer.consumer = 'agents'
            WHERE connection.project_id = $1
              AND connection.provider = $2
              AND connection.version = $3
              AND connection.inventory_version = $4
              AND connection.state = 'active'
              AND model.model_id = $5
            FOR SHARE OF connection, model, credential, consumer
            """,
            project_id,
            selection.provider,
            selection.connection_version,
            selection.inventory_version,
            selection.model,
        )
        if row is None:
            raise AgentsSetupConflictError(
                f"The {tier} model selection is stale or unavailable"
            )
        if str(row["credential_state"]) != "active":
            raise AgentsSetupConflictError(
                f"The {tier} provider credential is unavailable"
            )
        if (
            str(row["connection_catalog_version"]) != CATALOG_VERSION
            or str(row["model_catalog_version"]) != CATALOG_VERSION
        ):
            raise AgentsSetupConflictError(
                f"The {tier} model catalog is stale"
            )
        if tier not in {str(value) for value in row["supported_tiers"]}:
            raise AgentsSetupValidationError(
                f"The selected model is not eligible for the {tier} tier"
            )
        model = catalog_model(selection.provider, selection.model)
        if (
            model is None
            or model.catalog_version != str(row["model_catalog_version"])
        ):
            raise AgentsSetupConflictError(
                f"The {tier} model catalog is stale"
            )
        return model

    async def put(
        self,
        project_id: str,
        *,
        fast_model: ModelSelection,
        reasoning_model: ModelSelection,
        expected_version: int,
        actor_user_id: UUID,
    ) -> AgentsProjectSetup:
        selections: tuple[tuple[SetupTier, ModelSelection], ...] = (
            ("fast", fast_model),
            ("reasoning", reasoning_model),
        )
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"apdl:agents-setup:{project_id}",
                )
                authority = await self._management_authority(
                    conn,
                    project_id,
                    actor_user_id,
                    lock=True,
                )
                if authority == "none":
                    raise AgentsSetupAuthorizationError(
                        "Agents setup requires project ownership or delegated "
                        "agents:manage and credentials:manage roles"
                    )
                policy = await self._policy(conn, project_id, lock=True)
                if policy is None:
                    raise AgentsSetupNotFoundError(
                        "Agents project setup was not found"
                    )
                if int(policy["version"]) != expected_version:
                    raise AgentsSetupConflictError(
                        "The Agents setup version changed"
                    )
                previous_assignments = await self._assignment_rows(
                    conn, project_id
                )
                reviewed = {
                    tier: await self._validate_selection(
                        conn,
                        project_id=project_id,
                        tier=tier,
                        selection=selection,
                    )
                    for tier, selection in selections
                }
                residencies = {
                    model.data_residency for model in reviewed.values()
                }
                if len(residencies) != 1:
                    raise AgentsSetupValidationError(
                        "Selected models must share one reviewed data residency"
                    )
                previous_snapshot = _snapshot(
                    state=str(policy["state"]),
                    version=int(policy["version"]),
                    project_budget=int(
                        policy["project_daily_cost_limit_usd_micros"]
                    ),
                    run_budget=int(policy["run_cost_limit_usd_micros"]),
                    assignments=previous_assignments,
                )
                await conn.execute(
                    """
                    DELETE FROM llm_project_model_assignments
                    WHERE project_id = $1
                    """,
                    project_id,
                )
                await conn.execute(
                    """
                    DELETE FROM llm_project_provider_policies
                    WHERE project_id = $1
                    """,
                    project_id,
                )
                inserted_policies: set[tuple[str, str]] = set()
                for tier, selection in selections:
                    model = reviewed[tier]
                    identity = (selection.provider, selection.model)
                    if identity not in inserted_policies:
                        await conn.execute(
                            """
                            INSERT INTO llm_project_provider_policies (
                                project_id, provider, model, endpoint_url,
                                data_residency,
                                allowed_data_classifications,
                                input_cost_per_million_tokens_usd_micros,
                                output_cost_per_million_tokens_usd_micros,
                                enabled
                            ) VALUES (
                                $1, $2, $3, $4, $5, $6, $7, $8, TRUE
                            )
                            """,
                            project_id,
                            selection.provider,
                            selection.model,
                            provider_runtime_endpoint(selection.provider),
                            model.data_residency,
                            list(model.allowed_data_classifications),
                            model.input_cost_per_million_tokens_usd_micros,
                            model.output_cost_per_million_tokens_usd_micros,
                        )
                        inserted_policies.add(identity)
                    await conn.execute(
                        """
                        INSERT INTO llm_project_model_assignments (
                            project_id, tier, provider, model,
                            model_catalog_version
                        ) VALUES ($1, $2, $3, $4, $5)
                        """,
                        project_id,
                        tier,
                        selection.provider,
                        selection.model,
                        model.catalog_version,
                    )
                next_version = expected_version + 1
                await conn.execute(
                    """
                    UPDATE llm_project_policies
                    SET state = 'active',
                        version = $2,
                        required_data_residency = $3,
                        allow_cross_vendor_retry = FALSE,
                        project_daily_cost_limit_usd_micros = $4,
                        run_cost_limit_usd_micros = $5,
                        activated_by_actor_user_id = $6,
                        activated_at = NOW(),
                        deactivated_by_actor_user_id = NULL,
                        deactivation_reason = NULL,
                        deactivated_at = NULL,
                        updated_at = NOW()
                    WHERE project_id = $1
                    """,
                    project_id,
                    next_version,
                    next(iter(residencies)),
                    DEFAULT_PROJECT_DAILY_COST_LIMIT_USD_MICROS,
                    DEFAULT_RUN_COST_LIMIT_USD_MICROS,
                    actor_user_id,
                )
                if (
                    authority == "owner"
                    and int(policy["version"]) == 0
                    and str(policy["state"]) == "inactive"
                ):
                    owner_membership = await conn.fetchrow(
                        """
                        SELECT membership.roles AS previous_roles,
                               account.email,
                               apdl_canonical_admin_roles(
                                   membership.roles || ARRAY[
                                       'agents:run', 'agents:manage'
                                   ]::TEXT[]
                               ) AS next_roles
                        FROM admin_user_projects AS membership
                        JOIN admin_users AS account
                          ON account.user_id = membership.user_id
                        WHERE membership.project_id = $1
                          AND membership.user_id = $2
                        FOR UPDATE OF membership
                        """,
                        project_id,
                        actor_user_id,
                    )
                    if owner_membership is None:
                        raise AgentsSetupAuthorizationError(
                            "The current project owner membership is unavailable"
                        )
                    previous_roles = list(
                        owner_membership["previous_roles"]
                    )
                    next_roles = list(owner_membership["next_roles"])
                    if previous_roles != next_roles:
                        await conn.execute(
                            """
                            UPDATE admin_user_projects
                            SET roles = $3
                            WHERE project_id = $1 AND user_id = $2
                            """,
                            project_id,
                            actor_user_id,
                            next_roles,
                        )
                        await conn.execute(
                            """
                            INSERT INTO admin_project_membership_audit (
                                project_id, action, actor_user_id,
                                subject_user_id, subject_email,
                                previous_roles, new_roles
                            ) VALUES (
                                $1, 'roles_replace', $2, $2, $3, $4, $5
                            )
                            """,
                            project_id,
                            actor_user_id,
                            str(owner_membership["email"]),
                            previous_roles,
                            next_roles,
                        )
                next_snapshot = _snapshot(
                    state="active",
                    version=next_version,
                    project_budget=(
                        DEFAULT_PROJECT_DAILY_COST_LIMIT_USD_MICROS
                    ),
                    run_budget=DEFAULT_RUN_COST_LIMIT_USD_MICROS,
                    assignments=selections,
                )
                action = (
                    "activate"
                    if str(policy["state"]) == "inactive"
                    else "reconfigure"
                )
                await conn.execute(
                    """
                    INSERT INTO llm_project_setup_audit (
                        project_id, action, outcome, actor_user_id,
                        setup_version, previous_setup, next_setup
                    ) VALUES (
                        $1, $2, 'succeeded', $3, $4, $5::jsonb, $6::jsonb
                    )
                    """,
                    project_id,
                    action,
                    actor_user_id,
                    next_version,
                    json.dumps(previous_snapshot, sort_keys=True),
                    json.dumps(next_snapshot, sort_keys=True),
                )
        return await self.get(project_id, actor_user_id=actor_user_id)

    async def deactivate(
        self,
        project_id: str,
        *,
        expected_version: int,
        actor_user_id: UUID,
        reason: str,
    ) -> AgentsProjectSetup:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"apdl:agents-setup:{project_id}",
                )
                authority = await self._management_authority(
                    conn,
                    project_id,
                    actor_user_id,
                    lock=True,
                )
                if authority == "none":
                    raise AgentsSetupAuthorizationError(
                        "Agents setup requires project ownership or delegated "
                        "agents:manage and credentials:manage roles"
                    )
                policy = await self._policy(conn, project_id, lock=True)
                if policy is None:
                    raise AgentsSetupNotFoundError(
                        "Agents project setup was not found"
                    )
                if (
                    int(policy["version"]) != expected_version
                    or str(policy["state"]) != "active"
                ):
                    raise AgentsSetupConflictError(
                        "The Agents setup version or state changed"
                    )
                previous_assignments = await self._assignment_rows(
                    conn, project_id
                )
                previous_snapshot = _snapshot(
                    state="active",
                    version=expected_version,
                    project_budget=int(
                        policy["project_daily_cost_limit_usd_micros"]
                    ),
                    run_budget=int(policy["run_cost_limit_usd_micros"]),
                    assignments=previous_assignments,
                )
                next_version = expected_version + 1
                await conn.execute(
                    """
                    UPDATE llm_project_policies
                    SET state = 'inactive',
                        version = $2,
                        deactivated_by_actor_user_id = $3,
                        deactivation_reason = $4,
                        deactivated_at = NOW(),
                        updated_at = NOW()
                    WHERE project_id = $1
                    """,
                    project_id,
                    next_version,
                    actor_user_id,
                    reason,
                )
                await conn.execute(
                    """
                    UPDATE custom_agent_test_runs
                    SET status = 'failed',
                        error = 'Agents project setup was deactivated',
                        finished_at = NOW(),
                        lease_expires_at = LEAST(
                            lease_expires_at, NOW()
                        )
                    WHERE project_id = $1 AND status = 'running'
                    """,
                    project_id,
                )
                await conn.execute(
                    """
                    UPDATE agent_approval_effects AS effect
                    SET status = 'manual_intervention',
                        last_error =
                            'Agents project setup was deactivated before effect claim',
                        lease_owner_id = NULL,
                        lease_expires_at = NULL,
                        completed_at = NOW(),
                        updated_at = NOW()
                    FROM agent_runs AS run
                    WHERE run.project_id = $1
                      AND effect.run_id = run.run_id
                      AND effect.project_id = run.project_id
                      AND effect.status = 'queued'
                    """,
                    project_id,
                )
                await conn.execute(
                    """
                    UPDATE agent_approval_commands AS command
                    SET status = 'manual_intervention',
                        last_error =
                            'Agents project setup was deactivated',
                        completed_at = NOW(),
                        updated_at = NOW()
                    FROM agent_runs AS run
                    WHERE run.project_id = $1
                      AND command.run_id = run.run_id
                      AND command.project_id = run.project_id
                      AND command.status IN ('queued', 'processing')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM agent_approval_effects AS effect
                          WHERE effect.command_id = command.command_id
                            AND effect.status IN (
                                'processing', 'retryable_failed'
                            )
                      )
                    """,
                    project_id,
                )
                await conn.execute(
                    """
                    UPDATE agent_runs AS run
                    SET status = CASE
                            WHEN EXISTS (
                                SELECT 1
                                FROM agent_approval_effects AS effect
                                WHERE effect.run_id = run.run_id
                                  AND effect.project_id = run.project_id
                                  AND effect.status IN (
                                      'processing', 'retryable_failed'
                                  )
                            ) THEN 'cancelling'
                            ELSE 'cancelled'
                        END,
                        phase = CASE
                            WHEN EXISTS (
                                SELECT 1
                                FROM agent_approval_effects AS effect
                                WHERE effect.run_id = run.run_id
                                  AND effect.project_id = run.project_id
                                  AND effect.status IN (
                                      'processing', 'retryable_failed'
                                  )
                            ) THEN 'cancellation_reconciliation'
                            ELSE 'agents_setup_deactivated'
                        END,
                        lease_owner_id = CASE
                            WHEN EXISTS (
                                SELECT 1
                                FROM agent_approval_effects AS effect
                                WHERE effect.run_id = run.run_id
                                  AND effect.project_id = run.project_id
                                  AND effect.status IN (
                                      'processing', 'retryable_failed'
                                  )
                            ) THEN run.lease_owner_id
                            ELSE NULL
                        END,
                        lease_expires_at = CASE
                            WHEN EXISTS (
                                SELECT 1
                                FROM agent_approval_effects AS effect
                                WHERE effect.run_id = run.run_id
                                  AND effect.project_id = run.project_id
                                  AND effect.status IN (
                                      'processing', 'retryable_failed'
                                  )
                            ) THEN run.lease_expires_at
                            ELSE NULL
                        END,
                        updated_at = NOW()
                    WHERE run.project_id = $1
                      AND run.execution_lane_project_id = run.project_id
                    """,
                    project_id,
                )
                next_snapshot = _snapshot(
                    state="inactive",
                    version=next_version,
                    project_budget=int(
                        policy["project_daily_cost_limit_usd_micros"]
                    ),
                    run_budget=int(policy["run_cost_limit_usd_micros"]),
                    assignments=previous_assignments,
                )
                await conn.execute(
                    """
                    INSERT INTO llm_project_setup_audit (
                        project_id, action, outcome, actor_user_id,
                        setup_version, previous_setup, next_setup, reason
                    ) VALUES (
                        $1, 'deactivate', 'succeeded', $2, $3,
                        $4::jsonb, $5::jsonb, $6
                    )
                    """,
                    project_id,
                    actor_user_id,
                    next_version,
                    json.dumps(previous_snapshot, sort_keys=True),
                    json.dumps(next_snapshot, sort_keys=True),
                    reason,
                )
        return await self.get(project_id, actor_user_id=actor_user_id)
