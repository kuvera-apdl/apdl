"""Immutable changeset routing snapshots and pre-egress attempt ledger."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from app.config import (
    codegen_revision,
    codegen_rollout_stage,
)
from app.editor.environment import (
    codegen_tenant_behavior_configuration_sha256,
)
from app.llm.contracts import (
    LlmAssignmentSnapshot,
    LlmExecutionSnapshot,
    LlmRuntimeBinding,
    Phase,
    PreparedLlmAttempt,
    Provider,
    Role,
)
from app.llm.provider_catalog import CATALOG_VERSION, runtime_model
from app.models.connection import RepositoryTarget
from app.models.execution import PublicationStage
from app.publication import (
    DevelopmentPublicationAuthorization,
    PUBLICATION_AUTHORIZATION_ADAPTER,
    TenantPublicationAuthorization,
)
from app.store.llm_credentials import (
    CredentialDecryptionError,
    CredentialNotFoundError,
    CredentialStoreError,
    ProjectCredentialStore,
    validate_scope,
)


class LlmRoutingError(RuntimeError):
    """Base class for secret-free project routing failures."""


class LlmRoutingUnavailableError(LlmRoutingError):
    """The project has no complete executable Codegen LLM assignment."""


class LlmAttemptConflictError(LlmRoutingError):
    """An attempt transition lost its expected lifecycle or credential."""


_ROLE_BY_PHASE: dict[Phase, Role] = {
    "brief": "helper",
    "edit": "editor",
    "review": "helper",
    "repair": "editor",
}

AttemptBlockClassification = Literal[
    "changeset_unavailable",
    "execution_authority_unavailable",
    "repository_authority_unavailable",
    "rollout_authority_unavailable",
    "credential_unavailable",
    "credential_replaced",
    "credential_revoked",
    "credential_authentication",
    "connection_unavailable",
    "model_unavailable",
    "cancelled",
    "unknown",
]

# Runtime queries may row-lock the Codegen projections, but vault authority is
# deliberately read-only to this service. Provider-pair advisory locks and the
# just-in-time vault access boundary serialize and revalidate credential use.


def _snapshot_runtime_is_current(snapshot: LlmExecutionSnapshot) -> bool:
    stage = codegen_rollout_stage()
    return (
        snapshot.codegen_revision == codegen_revision()
        and snapshot.behavior_configuration_sha256
        == codegen_tenant_behavior_configuration_sha256()
        and snapshot.rollout_stage == stage.value
        and all(
            assignment.catalog_version == CATALOG_VERSION
            for assignment in snapshot.assignments
        )
    )


def _phase_status_is_live(phase: Phase, status: str) -> bool:
    if phase == "edit":
        return status == "editing"
    if phase == "repair":
        return status == "pr_open"
    return status in {"editing", "pr_open"}


def _publication_authorizes_snapshot(
    value: object,
    snapshot: LlmExecutionSnapshot,
) -> bool:
    try:
        raw = (
            value
            if isinstance(value, (str, bytes, bytearray))
            else json.dumps(value)
        )
        publication = PUBLICATION_AUTHORIZATION_ADAPTER.validate_json(raw)
    except (TypeError, ValueError):
        return False
    if isinstance(publication, TenantPublicationAuthorization):
        return (
            publication.decision.allowed
            and publication.decision.publish_branch
            and publication.request.execution_snapshot == snapshot
            and publication.request.execution_snapshot_sha256
            == snapshot.evidence_sha256()
        )
    if not isinstance(publication, DevelopmentPublicationAuthorization):
        return False
    try:
        editor_model = runtime_model(
            snapshot.assignment("editor").provider,
            snapshot.assignment("editor").model_id,
        ).litellm_model
    except ValueError:
        return False
    return (
        snapshot.rollout_stage == PublicationStage.development_pr.value
        and publication.decision.allowed
        and publication.decision.publish_branch
        and publication.request.requested_stage.value == snapshot.rollout_stage
        and publication.request.codegen_revision == snapshot.codegen_revision
        and publication.request.model == editor_model
    )


async def _changeset_authority_failure(
    conn: Any,
    *,
    row: Any,
    snapshot: LlmExecutionSnapshot,
    phase: Phase,
) -> tuple[AttemptBlockClassification, str] | None:
    if (
        str(row["project_id"]) != snapshot.project_id
        or bool(row["repository_target_quarantined"])
        or not _phase_status_is_live(phase, str(row["status"]))
    ):
        return (
            "changeset_unavailable",
            "Changeset is not live for this LLM phase",
        )
    if not _snapshot_runtime_is_current(snapshot):
        return (
            "rollout_authority_unavailable",
            "Changeset execution snapshot does not match this deployment",
        )
    if not _publication_authorizes_snapshot(
        row["publication_authorization"], snapshot
    ):
        return (
            "rollout_authority_unavailable",
            "Changeset has no current rollout publication authority",
        )
    execution_authority = await conn.fetchrow(
        """
        SELECT project_id
        FROM admin_project_execution_authorizations
        WHERE project_id = $1
        FOR SHARE
        """,
        snapshot.project_id,
    )
    if execution_authority is None:
        return (
            "execution_authority_unavailable",
            "Project execution authority is unavailable",
        )
    repository_authority = await conn.fetchrow(
        """
        SELECT connection.project_id
        FROM codegen_connections AS connection
        JOIN github_repository_grants AS grant_record
          ON grant_record.project_id = connection.project_id
         AND grant_record.grant_id = connection.grant_id
        WHERE connection.project_id = $1
          AND connection.grant_id = $2
          AND grant_record.status = 'active'
          AND grant_record.verified_at IS NOT NULL
          AND grant_record.revoked_at IS NULL
          AND grant_record.repository_id = $3
          AND grant_record.installation_id = $4
          AND grant_record.repository_full_name = $5
        FOR SHARE OF connection, grant_record
        """,
        snapshot.project_id,
        snapshot.repository_grant_id,
        snapshot.repository_id,
        snapshot.repository_installation_id,
        snapshot.repository_full_name,
    )
    if repository_authority is None:
        return (
            "repository_authority_unavailable",
            "Repository execution authority is unavailable",
        )
    return None


async def _provider_authority(
    conn: Any,
    *,
    project_id: str,
    provider: Provider,
    assignment: LlmAssignmentSnapshot,
    role: Role,
) -> tuple[UUID | None, int | None, AttemptBlockClassification | None]:
    connection = await conn.fetchrow(
        """
        SELECT version, inventory_version, catalog_version, state, credential_id
        FROM codegen_project_provider_connections
        WHERE project_id = $1 AND provider = $2
        FOR SHARE
        """,
        project_id,
        provider,
    )
    if connection is None or str(connection["state"]) != "active":
        return None, None, "connection_unavailable"
    if (
        int(connection["version"]) != assignment.connection_version
        or int(connection["inventory_version"]) != assignment.inventory_version
        or str(connection["catalog_version"]) != assignment.catalog_version
    ):
        return None, None, "connection_unavailable"
    model = await conn.fetchrow(
        """
        SELECT model_id
        FROM codegen_project_provider_models
        WHERE project_id = $1
          AND provider = $2
          AND model_id = $3
          AND connection_version = $4
          AND inventory_version = $5
          AND catalog_version = $6
          AND $7 = ANY(supported_roles)
        FOR SHARE
        """,
        project_id,
        provider,
        assignment.model_id,
        assignment.connection_version,
        assignment.inventory_version,
        assignment.catalog_version,
        role,
    )
    if model is None:
        return None, None, "model_unavailable"
    credential = await conn.fetchrow(
        """
        SELECT credential_id, credential_version, state
        FROM llm_vault_provider_credentials
        WHERE credential_id = $1
          AND project_id = $2
          AND provider = $3
        """,
        connection["credential_id"],
        project_id,
        provider,
    )
    if credential is None:
        return None, None, "credential_unavailable"
    state = str(credential["state"])
    if state == "revoked":
        return None, None, "credential_revoked"
    if state == "replaced":
        return None, None, "credential_replaced"
    if state != "active":
        return None, None, "credential_unavailable"
    return (
        UUID(str(credential["credential_id"])),
        int(credential["credential_version"]),
        None,
    )


async def _credential_failure_classification(
    pool: Any,
    *,
    project_id: str,
    provider: Provider,
    credential_id: UUID,
) -> AttemptBlockClassification:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ProjectCredentialStore.lock_pair(conn, project_id, provider)
            state = await conn.fetchval(
                """
                SELECT state
                FROM llm_vault_provider_credentials
                WHERE credential_id = $1
                  AND project_id = $2
                  AND provider = $3
                """,
                credential_id,
                project_id,
                provider,
            )
    if state == "revoked":
        return "credential_revoked"
    if state == "replaced":
        return "credential_replaced"
    return "credential_unavailable"


def _assignment(row: Any) -> LlmAssignmentSnapshot:
    return LlmAssignmentSnapshot(
        schema_version="codegen_llm_assignment_snapshot@1",
        role=str(row["role"]),
        provider=str(row["provider"]),
        model_id=str(row["model_id"]),
        assignment_version=int(row["assignment_version"]),
        connection_version=int(row["connection_version"]),
        inventory_version=int(row["inventory_version"]),
        catalog_version=str(row["catalog_version"]),
        context_window_tokens=int(row["context_window_tokens"]),
        supports_tool_calling=bool(row["supports_tool_calling"]),
        supports_structured_output=bool(row["supports_structured_output"]),
        input_cost_per_million_tokens_usd_micros=int(
            row["input_cost_per_million_tokens_usd_micros"]
        ),
        output_cost_per_million_tokens_usd_micros=int(
            row["output_cost_per_million_tokens_usd_micros"]
        ),
    )


async def capture_execution_snapshot(
    conn: Any,
    *,
    project_id: str,
    repository_target: RepositoryTarget,
) -> LlmExecutionSnapshot:
    """Read both current assignments in the caller's admission transaction."""
    if repository_target.project_id != project_id:
        raise ValueError("LLM snapshot project does not match repository grant")
    rows = await conn.fetch(
        """
        SELECT assignment.role, assignment.provider, assignment.model_id,
               assignment.assignment_version, assignment.connection_version,
               assignment.inventory_version, assignment.catalog_version,
               model.context_window_tokens, model.supports_tool_calling,
               model.supports_structured_output,
               model.input_cost_per_million_tokens_usd_micros,
               model.output_cost_per_million_tokens_usd_micros
        FROM codegen_project_model_assignments AS assignment
        JOIN codegen_project_provider_connections AS connection
          ON connection.project_id = assignment.project_id
         AND connection.provider = assignment.provider
         AND connection.version = assignment.connection_version
         AND connection.inventory_version = assignment.inventory_version
         AND connection.catalog_version = assignment.catalog_version
         AND connection.state = 'active'
        JOIN codegen_project_provider_models AS model
          ON model.project_id = assignment.project_id
         AND model.provider = assignment.provider
         AND model.model_id = assignment.model_id
         AND model.connection_version = assignment.connection_version
         AND model.inventory_version = assignment.inventory_version
         AND model.catalog_version = assignment.catalog_version
         AND assignment.role = ANY(model.supported_roles)
        JOIN llm_vault_provider_credentials AS credential
          ON credential.credential_id = connection.credential_id
         AND credential.project_id = connection.project_id
         AND credential.provider = connection.provider
         AND credential.state = 'active'
        JOIN llm_vault_connection_consumers AS consumer
          ON consumer.connection_id = credential.connection_id
         AND consumer.project_id = credential.project_id
         AND consumer.provider = credential.provider
         AND consumer.consumer = 'codegen'
        WHERE assignment.project_id = $1
        ORDER BY CASE assignment.role WHEN 'editor' THEN 0 ELSE 1 END
        FOR SHARE OF assignment, connection, model
        """,
        project_id,
    )
    assignments = tuple(_assignment(row) for row in rows)
    if len(assignments) != 2 or tuple(item.role for item in assignments) != (
        "editor",
        "helper",
    ):
        raise LlmRoutingUnavailableError(
            "Project requires active editor and helper Codegen model assignments"
        )
    snapshot = LlmExecutionSnapshot(
        schema_version="codegen_llm_execution_snapshot@2",
        project_id=project_id,
        repository_grant_id=repository_target.grant_id,
        repository_id=repository_target.repository_id,
        repository_installation_id=repository_target.installation_id,
        repository_full_name=repository_target.repository_full_name,
        codegen_revision=codegen_revision(),
        behavior_configuration_sha256=(
            codegen_tenant_behavior_configuration_sha256()
        ),
        rollout_stage=codegen_rollout_stage().value,
        assignments=cast(
            tuple[LlmAssignmentSnapshot, LlmAssignmentSnapshot], assignments
        ),
    )
    return snapshot


async def load_execution_snapshot(
    pool: Any, changeset_id: str
) -> LlmExecutionSnapshot:
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT llm_execution_snapshot
            FROM codegen_changesets
            WHERE changeset_id = $1
            """,
            changeset_id,
        )
    if value is None:
        raise LlmRoutingUnavailableError(
            "Changeset has no immutable Codegen LLM execution snapshot"
        )
    raw = value if isinstance(value, str) else json.dumps(value)
    return LlmExecutionSnapshot.model_validate_json(raw)


async def assign_project_models(
    pool: Any,
    *,
    project_id: str,
    editor_provider: str,
    editor_model_id: str,
    helper_provider: str,
    helper_model_id: str,
    actor: str,
) -> tuple[LlmAssignmentSnapshot, LlmAssignmentSnapshot]:
    """Atomically replace the project's exact editor/helper assignments."""
    editor_provider = validate_scope(project_id, editor_provider)
    helper_provider = validate_scope(project_id, helper_provider)
    requested = (
        ("editor", editor_provider, editor_model_id),
        ("helper", helper_provider, helper_model_id),
    )
    if (
        not actor
        or actor != actor.strip()
        or len(actor) > 512
        or "\r" in actor
        or "\n" in actor
    ):
        raise ValueError("actor must be 1 to 512 characters without line breaks")
    assignments: list[LlmAssignmentSnapshot] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"apdl:codegen-llm-assignments:{project_id}",
            )
            for role, provider, model_id in requested:
                row = await conn.fetchrow(
                    """
                    SELECT connection.version AS connection_version,
                           connection.inventory_version,
                           connection.catalog_version,
                           model.context_window_tokens,
                           model.supports_tool_calling,
                           model.supports_structured_output,
                           model.input_cost_per_million_tokens_usd_micros,
                           model.output_cost_per_million_tokens_usd_micros,
                           COALESCE(assignment.assignment_version, 0) + 1
                               AS assignment_version
                    FROM codegen_project_provider_connections AS connection
                    JOIN codegen_project_provider_models AS model
                      ON model.project_id = connection.project_id
                     AND model.provider = connection.provider
                     AND model.connection_version = connection.version
                     AND model.inventory_version = connection.inventory_version
                     AND model.catalog_version = connection.catalog_version
                    LEFT JOIN codegen_project_model_assignments AS assignment
                      ON assignment.project_id = connection.project_id
                     AND assignment.role = $4
                    WHERE connection.project_id = $1
                      AND connection.provider = $2
                      AND connection.state = 'active'
                      AND model.model_id = $3
                      AND $4 = ANY(model.supported_roles)
                    FOR SHARE OF connection, model
                    """,
                    project_id,
                    provider,
                    model_id,
                    role,
                )
                if row is None:
                    raise LlmRoutingUnavailableError(
                        f"{role} assignment requires an eligible current model"
                    )
                await conn.execute(
                    """
                    INSERT INTO codegen_project_model_assignments (
                        project_id, role, provider, model_id,
                        assignment_version, connection_version,
                        inventory_version, catalog_version, assigned_by_actor
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (project_id, role) DO UPDATE
                    SET provider = EXCLUDED.provider,
                        model_id = EXCLUDED.model_id,
                        assignment_version = EXCLUDED.assignment_version,
                        connection_version = EXCLUDED.connection_version,
                        inventory_version = EXCLUDED.inventory_version,
                        catalog_version = EXCLUDED.catalog_version,
                        assigned_by_actor = EXCLUDED.assigned_by_actor,
                        assigned_at = NOW()
                    """,
                    project_id,
                    role,
                    provider,
                    model_id,
                    int(row["assignment_version"]),
                    int(row["connection_version"]),
                    int(row["inventory_version"]),
                    str(row["catalog_version"]),
                    actor,
                )
                assignments.append(
                    LlmAssignmentSnapshot(
                        schema_version="codegen_llm_assignment_snapshot@1",
                        role=role,
                        provider=provider,
                        model_id=model_id,
                        assignment_version=int(row["assignment_version"]),
                        connection_version=int(row["connection_version"]),
                        inventory_version=int(row["inventory_version"]),
                        catalog_version=str(row["catalog_version"]),
                        context_window_tokens=int(row["context_window_tokens"]),
                        supports_tool_calling=bool(row["supports_tool_calling"]),
                        supports_structured_output=bool(
                            row["supports_structured_output"]
                        ),
                        input_cost_per_million_tokens_usd_micros=int(
                            row[
                                "input_cost_per_million_tokens_usd_micros"
                            ]
                        ),
                        output_cost_per_million_tokens_usd_micros=int(
                            row[
                                "output_cost_per_million_tokens_usd_micros"
                            ]
                        ),
                    )
                )
    return cast(
        tuple[LlmAssignmentSnapshot, LlmAssignmentSnapshot],
        tuple(assignments),
    )


async def _insert_blocked_attempt(
    conn: Any,
    *,
    attempt_id: UUID,
    project_id: str,
    changeset_id: str,
    phase: Phase,
    role: Role,
    attempt_sequence: int,
    assignment: LlmAssignmentSnapshot,
    error_classification: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO codegen_llm_attempts (
            attempt_id, project_id, changeset_id, phase, role,
            attempt_sequence, provider, model_id, assignment_version,
            status, finished_at, error_classification
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9,
            'blocked', NOW(), $10
        )
        """,
        attempt_id,
        project_id,
        changeset_id,
        phase,
        role,
        attempt_sequence,
        assignment.provider,
        assignment.model_id,
        assignment.assignment_version,
        error_classification,
    )


async def _materialize_prepared_attempt(
    pool: Any,
    credential_store: ProjectCredentialStore,
    *,
    attempt_id: UUID,
    changeset_id: str,
    phase: Phase,
    role: Role,
    attempt_sequence: int,
    snapshot: LlmExecutionSnapshot,
    assignment: LlmAssignmentSnapshot,
    provider: Provider,
    credential_id: UUID,
    credential_version: int,
) -> PreparedLlmAttempt:
    try:
        credential = await credential_store.load_active(
            snapshot.project_id,
            provider,
            credential_id=credential_id,
            credential_version=credential_version,
            execution_id=str(attempt_id),
            purpose=f"codegen.{phase}",
        )
    except CredentialNotFoundError as exc:
        classification = await _credential_failure_classification(
            pool,
            project_id=snapshot.project_id,
            provider=provider,
            credential_id=credential_id,
        )
        await block_llm_attempt(
            pool,
            attempt_id=attempt_id,
            error_classification=classification,
        )
        raise LlmAttemptConflictError(
            "Provider credential changed before execution"
        ) from exc
    except (CredentialDecryptionError, CredentialStoreError) as exc:
        await block_llm_attempt(
            pool,
            attempt_id=attempt_id,
            error_classification="credential_authentication",
        )
        raise LlmRoutingUnavailableError(
            "Provider credential could not be authenticated"
        ) from exc
    if credential.credential_version != credential_version:
        await block_llm_attempt(
            pool,
            attempt_id=attempt_id,
            error_classification="credential_replaced",
        )
        raise LlmAttemptConflictError(
            "Provider credential changed before execution"
        )
    try:
        runtime = runtime_model(provider, assignment.model_id)
        return PreparedLlmAttempt(
            attempt_id=attempt_id,
            changeset_id=changeset_id,
            project_id=snapshot.project_id,
            phase=phase,
            attempt_sequence=attempt_sequence,
            binding=LlmRuntimeBinding(
                role=role,
                provider=provider,
                model_id=assignment.model_id,
                litellm_model=runtime.litellm_model,
                credential_environment_name=(
                    runtime.credential_environment_name
                ),
                endpoint_url=runtime.endpoint_url,
                assignment_version=assignment.assignment_version,
                credential_id=credential_id,
                credential_version=credential_version,
                input_cost_per_million_tokens_usd_micros=(
                    assignment.input_cost_per_million_tokens_usd_micros
                ),
                output_cost_per_million_tokens_usd_micros=(
                    assignment.output_cost_per_million_tokens_usd_micros
                ),
                api_key=credential.api_key,
            ),
        )
    except Exception:
        await block_llm_attempt(
            pool,
            attempt_id=attempt_id,
            error_classification="model_unavailable",
        )
        raise LlmRoutingUnavailableError(
            "Snapshot model runtime is unavailable"
        ) from None


async def _cleanup_interrupted_prepared_attempt(
    pool: Any,
    *,
    attempt_id: UUID,
    cancelled: bool,
) -> None:
    cleanup = asyncio.create_task(
        abandon_llm_attempt(
            pool,
            attempt_id=attempt_id,
            cancelled=cancelled,
        )
    )
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # Repeated cancellation must not let the caller unwind while a
            # durable ``prepared`` row is still live. Continue shielding the
            # same strongly referenced task until terminalization finishes.
            continue
        except Exception:
            # Preserve the original provider/cancellation failure. A database
            # outage may prevent terminalization, but must not rewrite causality.
            return
    try:
        cleanup.result()
    except BaseException:
        # Cleanup is best effort, including if the cleanup task itself was
        # cancelled. The exception that entered this barrier remains causal.
        return


async def prepare_llm_attempt(
    pool: Any,
    credential_store: ProjectCredentialStore,
    *,
    changeset_id: str,
    phase: Phase,
    attempt_sequence: int | None = None,
) -> PreparedLlmAttempt:
    """Bind one snapshot phase to the current credential before any egress."""
    if attempt_sequence is not None and attempt_sequence < 1:
        raise ValueError("attempt_sequence must be positive")
    role = _ROLE_BY_PHASE[phase]
    attempt_id = uuid4()
    blocked_error: str | None = None
    credential_id: UUID | None = None
    credential_version: int | None = None
    snapshot: LlmExecutionSnapshot | None = None
    assignment: LlmAssignmentSnapshot | None = None
    provider: Provider | None = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"apdl:codegen-llm-attempt:{changeset_id}:{phase}",
            )
            if attempt_sequence is None:
                attempt_sequence = int(
                    await conn.fetchval(
                        """
                        SELECT COALESCE(MAX(attempt_sequence), 0) + 1
                        FROM codegen_llm_attempts
                        WHERE changeset_id = $1 AND phase = $2
                        """,
                        changeset_id,
                        phase,
                    )
                )
            row = await conn.fetchrow(
                """
                SELECT project_id, status, repository_target_quarantined,
                       publication_authorization, llm_execution_snapshot
                FROM codegen_changesets
                WHERE changeset_id = $1
                FOR SHARE
                """,
                changeset_id,
            )
            if row is None or row["llm_execution_snapshot"] is None:
                raise LlmRoutingUnavailableError(
                    "Changeset has no immutable Codegen LLM execution snapshot"
                )
            raw = row["llm_execution_snapshot"]
            snapshot = LlmExecutionSnapshot.model_validate_json(
                raw if isinstance(raw, str) else json.dumps(raw)
            )
            assignment = snapshot.assignment(role)
            provider = cast(Provider, assignment.provider)
            failure = await _changeset_authority_failure(
                conn,
                row=row,
                snapshot=snapshot,
                phase=phase,
            )
            if failure is None:
                await ProjectCredentialStore.lock_pair(
                    conn, snapshot.project_id, provider
                )
                (
                    credential_id,
                    credential_version,
                    provider_failure,
                ) = await _provider_authority(
                    conn,
                    project_id=snapshot.project_id,
                    provider=provider,
                    assignment=assignment,
                    role=role,
                )
                if provider_failure is not None:
                    failure = (
                        provider_failure,
                        "Snapshot model has no current provider authority",
                    )
            if failure is not None:
                await _insert_blocked_attempt(
                    conn,
                    attempt_id=attempt_id,
                    project_id=snapshot.project_id,
                    changeset_id=changeset_id,
                    phase=phase,
                    role=role,
                    attempt_sequence=attempt_sequence,
                    assignment=assignment,
                    error_classification=failure[0],
                )
                blocked_error = failure[1]
            else:
                assert credential_id is not None
                assert credential_version is not None
                await conn.execute(
                    """
                    INSERT INTO codegen_llm_attempts (
                        attempt_id, project_id, changeset_id, phase, role,
                        attempt_sequence, provider, model_id,
                        assignment_version, credential_id,
                        credential_version, status
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        'prepared'
                    )
                    """,
                    attempt_id,
                    snapshot.project_id,
                    changeset_id,
                    phase,
                    role,
                    attempt_sequence,
                    provider,
                    assignment.model_id,
                    assignment.assignment_version,
                    credential_id,
                    credential_version,
                )
    if blocked_error is not None:
        raise LlmRoutingUnavailableError(blocked_error)
    assert snapshot is not None
    assert assignment is not None
    assert provider is not None
    assert attempt_sequence is not None
    assert credential_id is not None
    assert credential_version is not None
    try:
        return await _materialize_prepared_attempt(
            pool,
            credential_store,
            attempt_id=attempt_id,
            changeset_id=changeset_id,
            phase=phase,
            role=role,
            attempt_sequence=attempt_sequence,
            snapshot=snapshot,
            assignment=assignment,
            provider=provider,
            credential_id=credential_id,
            credential_version=credential_version,
        )
    except BaseException as exc:
        await _cleanup_interrupted_prepared_attempt(
            pool,
            attempt_id=attempt_id,
            cancelled=isinstance(exc, asyncio.CancelledError),
        )
        raise


async def mark_llm_egress(
    pool: Any,
    *,
    attempt: PreparedLlmAttempt,
) -> None:
    """Linearize current credential authority immediately before egress."""
    updated: object | None = None
    blocked = False
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ProjectCredentialStore.lock_pair(
                conn, attempt.project_id, attempt.binding.provider
            )
            attempt_row = await conn.fetchrow(
                """
                SELECT status, phase, role, provider, model_id,
                       assignment_version, credential_id, credential_version
                FROM codegen_llm_attempts
                WHERE attempt_id = $1
                  AND project_id = $2
                  AND changeset_id = $3
                FOR UPDATE
                """,
                attempt.attempt_id,
                attempt.project_id,
                attempt.changeset_id,
            )
            if (
                attempt_row is not None
                and str(attempt_row["status"]) == "prepared"
            ):
                changeset_row = await conn.fetchrow(
                    """
                    SELECT project_id, status, repository_target_quarantined,
                           publication_authorization, llm_execution_snapshot
                    FROM codegen_changesets
                    WHERE changeset_id = $1
                    FOR SHARE
                    """,
                    attempt.changeset_id,
                )
                failure: tuple[
                    AttemptBlockClassification, str
                ] | None = None
                if (
                    changeset_row is None
                    or changeset_row["llm_execution_snapshot"] is None
                ):
                    failure = (
                        "changeset_unavailable",
                        "Changeset execution authority is unavailable",
                    )
                else:
                    raw = changeset_row["llm_execution_snapshot"]
                    snapshot = LlmExecutionSnapshot.model_validate_json(
                        raw if isinstance(raw, str) else json.dumps(raw)
                    )
                    phase = cast(Phase, str(attempt_row["phase"]))
                    role = _ROLE_BY_PHASE[phase]
                    assignment = snapshot.assignment(role)
                    failure = await _changeset_authority_failure(
                        conn,
                        row=changeset_row,
                        snapshot=snapshot,
                        phase=phase,
                    )
                    if failure is None and (
                        str(attempt_row["role"]) != role
                        or str(attempt_row["provider"])
                        != attempt.binding.provider
                        or str(attempt_row["model_id"])
                        != attempt.binding.model_id
                        or int(attempt_row["assignment_version"])
                        != attempt.binding.assignment_version
                        or UUID(str(attempt_row["credential_id"]))
                        != attempt.binding.credential_id
                        or int(attempt_row["credential_version"])
                        != attempt.binding.credential_version
                    ):
                        failure = (
                            "changeset_unavailable",
                            "LLM attempt authority does not match its lease",
                        )
                    exact_credential_state = await conn.fetchval(
                        """
                        SELECT state
                        FROM llm_vault_provider_credentials
                        WHERE credential_id = $1
                          AND project_id = $2
                          AND provider = $3
                          AND credential_version = $4
                        """,
                        attempt.binding.credential_id,
                        attempt.project_id,
                        attempt.binding.provider,
                        attempt.binding.credential_version,
                    )
                    if failure is None and exact_credential_state != "active":
                        classification: AttemptBlockClassification
                        if exact_credential_state == "revoked":
                            classification = "credential_revoked"
                        elif exact_credential_state == "replaced":
                            classification = "credential_replaced"
                        else:
                            classification = "credential_unavailable"
                        failure = (
                            classification,
                            "Provider credential changed before egress",
                        )
                    if failure is None:
                        (
                            current_credential_id,
                            current_credential_version,
                            provider_failure,
                        ) = await _provider_authority(
                            conn,
                            project_id=attempt.project_id,
                            provider=attempt.binding.provider,
                            assignment=assignment,
                            role=role,
                        )
                        if provider_failure is not None:
                            failure = (
                                provider_failure,
                                "Provider authority changed before egress",
                            )
                        elif (
                            current_credential_id
                            != attempt.binding.credential_id
                            or current_credential_version
                            != attempt.binding.credential_version
                        ):
                            failure = (
                                "credential_replaced",
                                "Provider credential changed before egress",
                            )
                if failure is None:
                    updated = await conn.fetchval(
                        """
                        UPDATE codegen_llm_attempts
                        SET status = 'in_flight', egress_at = NOW()
                        WHERE attempt_id = $1 AND status = 'prepared'
                        RETURNING attempt_id
                        """,
                        attempt.attempt_id,
                    )
                else:
                    blocked_id = await conn.fetchval(
                        """
                        UPDATE codegen_llm_attempts
                        SET status = 'blocked', finished_at = NOW(),
                            error_classification = $2
                        WHERE attempt_id = $1 AND status = 'prepared'
                        RETURNING attempt_id
                        """,
                        attempt.attempt_id,
                        failure[0],
                    )
                    blocked = blocked_id is not None
    if blocked:
        raise LlmAttemptConflictError(
            "LLM authority changed before egress"
        )
    if updated is None:
        raise LlmAttemptConflictError("LLM attempt is not prepared")


async def block_llm_attempt(
    pool: Any,
    *,
    attempt_id: UUID,
    error_classification: Literal[
        "changeset_unavailable",
        "execution_authority_unavailable",
        "repository_authority_unavailable",
        "rollout_authority_unavailable",
        "credential_unavailable",
        "credential_replaced",
        "credential_revoked",
        "credential_authentication",
        "connection_unavailable",
        "model_unavailable",
        "cancelled",
        "unknown",
    ],
) -> None:
    async with pool.acquire() as conn:
        updated = await conn.fetchval(
            """
            UPDATE codegen_llm_attempts
            SET status = 'blocked', finished_at = NOW(),
                error_classification = $2
            WHERE attempt_id = $1 AND status = 'prepared'
            RETURNING attempt_id
            """,
            attempt_id,
            error_classification,
        )
    if updated is None:
        raise LlmAttemptConflictError("LLM attempt is not prepared")


async def finish_llm_attempt(
    pool: Any,
    *,
    attempt_id: UUID,
    status: Literal["succeeded", "failed", "cancelled"],
    latency_ms: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd_micros: int | None = None,
    error_classification: str | None = None,
) -> None:
    if status == "succeeded" and error_classification is not None:
        raise ValueError("successful attempts cannot have an error classification")
    if status != "succeeded" and error_classification is None:
        raise ValueError("unsuccessful attempts require an error classification")
    async with pool.acquire() as conn:
        updated = await conn.fetchval(
            """
            UPDATE codegen_llm_attempts
            SET status = $2, finished_at = NOW(), latency_ms = $3,
                input_tokens = $4, output_tokens = $5,
                cost_usd_micros = $6, error_classification = $7
            WHERE attempt_id = $1 AND status = 'in_flight'
            RETURNING attempt_id
            """,
            attempt_id,
            status,
            latency_ms,
            input_tokens,
            output_tokens,
            cost_usd_micros,
            error_classification,
        )
    if updated is None:
        raise LlmAttemptConflictError("LLM attempt is not in flight")


async def abandon_llm_attempt(
    pool: Any,
    *,
    attempt_id: UUID,
    cancelled: bool,
) -> None:
    """Terminalize a broker-owned prepared or in-flight attempt exactly once."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE codegen_llm_attempts
            SET status = CASE
                    WHEN $2::BOOLEAN THEN 'cancelled'
                    WHEN status = 'prepared' THEN 'blocked'
                    ELSE 'failed'
                END,
                finished_at = NOW(),
                latency_ms = CASE
                    WHEN status = 'in_flight' THEN COALESCE(latency_ms, 0)
                    ELSE latency_ms
                END,
                error_classification = CASE
                    WHEN $2::BOOLEAN THEN 'cancelled'
                    ELSE 'unknown'
                END
            WHERE attempt_id = $1
              AND status IN ('prepared', 'in_flight')
            """,
            attempt_id,
            cancelled,
        )
