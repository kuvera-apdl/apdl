"""Persistence for changesets (``codegen_changesets``).

Reads and writes are explicit-column projections (no ``SELECT *`` into the
model). :func:`transition_changeset` enforces the lifecycle state machine inside
a row-locked transaction so concurrent updates cannot corrupt the status.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import asyncpg

from app.contracts.models import ContractBundle
from app.editor.prompts import bound_prompt_transcript
from app.inspection.models import DependencySlice, InspectionSnapshot
from app.models.changeset import (
    CI_SYNCABLE_STATUSES,
    Changeset,
    ChangesetStatus,
    TaskSpec,
    assert_transition,
)
from app.models.connection import RepositoryTarget
from app.models.observations import (
    CIRemediationStatus,
    ExternalCIStatus,
    GitHubPRStatus,
    PullRequestObservation,
)
from app.publication import (
    PUBLICATION_AUTHORIZATION_ADAPTER,
    PublicationAuthorizationRecord,
)
from app.requirements.models import RequirementLedger
from app.runtime.models import RuntimeAcceptancePlan, RuntimeEvidenceAssessment
from app.safety.policy import TenantCodegenConnectionPolicy
from app.semantic_review.models import ReviewVerdict
from app.store.jsonb import loads_jsonb
from app.verification.models import VerificationCoverage, VerificationPlan

#: Pre-PR pipeline states a running job actively drives (``queued`` excluded:
#: a queued row hasn't started, so a restart re-enqueues it rather than failing
#: it — see :func:`list_queued_changeset_ids`). The job runner uses in-process
#: background tasks, so a process restart orphans any changeset sitting here —
#: :func:`fail_stale_changesets` sweeps the stale ones.
_ACTIVE_STATUSES: tuple[ChangesetStatus, ...] = (
    ChangesetStatus.cloning,
    ChangesetStatus.editing,
    ChangesetStatus.pushing,
)


class ChangesetIdempotencyConflict(ValueError):
    """Raised when one key is reused for a different immutable request."""


def changeset_request_sha256(
    *,
    project_id: str,
    run_id: str | None,
    base_branch: str | None,
    task: dict[str, Any],
) -> str:
    """Hash the exact canonical client intent with JSON types preserved."""
    canonical = {
        "project_id": project_id,
        "run_id": run_id,
        "base_branch": base_branch,
        "task": TaskSpec.model_validate(task).model_dump(mode="json"),
    }
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prompts_from_row(row: asyncpg.Record) -> list[dict[str, Any]]:
    """The ``prompts`` column as a list, tolerant of rows that predate it.

    JSONB arrives as ``str`` from asyncpg and as a Python value from the test
    fakes; a row missing the column entirely (old fake fixtures) reads as empty.
    """
    try:
        value = row["prompts"]
    except KeyError:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    return value or []


def _optional_column(row: asyncpg.Record, key: str) -> Any:
    """A column's value, ``None`` for rows that predate it (old test fakes)."""
    try:
        return row[key]
    except KeyError:
        return None


def _contract_bundle_from_row(row: asyncpg.Record) -> ContractBundle | None:
    value = _optional_column(row, "contract_bundle")
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    return ContractBundle.model_validate(value)


def _requirement_ledger_from_row(row: asyncpg.Record) -> RequirementLedger | None:
    value = _optional_column(row, "requirement_ledger")
    if value is None:
        return None
    if isinstance(value, str):
        return RequirementLedger.model_validate_json(value)
    return RequirementLedger.model_validate_json(json.dumps(value))


def _inspection_snapshot_from_row(row: asyncpg.Record) -> InspectionSnapshot | None:
    value = _optional_column(row, "inspection_snapshot")
    if value is None:
        return None
    raw = value if isinstance(value, str) else json.dumps(value)
    return InspectionSnapshot.model_validate_json(raw)


def _dependency_slice_from_row(row: asyncpg.Record) -> DependencySlice | None:
    value = _optional_column(row, "dependency_slice")
    if value is None:
        return None
    raw = value if isinstance(value, str) else json.dumps(value)
    return DependencySlice.model_validate_json(raw)


def _verification_plan_from_row(row: asyncpg.Record) -> VerificationPlan | None:
    value = _optional_column(row, "verification_plan")
    if value is None:
        return None
    raw = value if isinstance(value, str) else json.dumps(value)
    return VerificationPlan.model_validate_json(raw)


def _verification_coverage_from_row(
    row: asyncpg.Record,
) -> VerificationCoverage | None:
    value = _optional_column(row, "verification_coverage")
    if value is None:
        return None
    raw = value if isinstance(value, str) else json.dumps(value)
    return VerificationCoverage.model_validate_json(raw)


def _review_verdict_from_row(row: asyncpg.Record) -> ReviewVerdict | None:
    value = _optional_column(row, "review_verdict")
    if value is None:
        return None
    raw = value if isinstance(value, str) else json.dumps(value)
    return ReviewVerdict.model_validate_json(raw)


def _runtime_acceptance_plan_from_row(
    row: asyncpg.Record,
) -> RuntimeAcceptancePlan | None:
    value = _optional_column(row, "runtime_acceptance_plan")
    if value is None:
        return None
    raw = value if isinstance(value, str) else json.dumps(value)
    return RuntimeAcceptancePlan.model_validate_json(raw)


def _runtime_evidence_assessment_from_row(
    row: asyncpg.Record,
) -> RuntimeEvidenceAssessment | None:
    value = _optional_column(row, "runtime_evidence_assessment")
    if value is None:
        return None
    raw = value if isinstance(value, str) else json.dumps(value)
    return RuntimeEvidenceAssessment.model_validate_json(raw)


def _publication_authorization_from_row(
    row: asyncpg.Record,
) -> PublicationAuthorizationRecord | None:
    value = _optional_column(row, "publication_authorization")
    if value is None:
        return None
    raw = value if isinstance(value, str) else json.dumps(value)
    return PUBLICATION_AUTHORIZATION_ADAPTER.validate_json(raw)


def _tenant_policy_snapshot_from_row(
    row: asyncpg.Record,
) -> TenantCodegenConnectionPolicy | None:
    value = _optional_column(row, "tenant_policy_snapshot")
    if value is None:
        return None
    raw = value if isinstance(value, str) else json.dumps(value)
    return TenantCodegenConnectionPolicy.model_validate_json(raw)


def _row_to_changeset(row: asyncpg.Record) -> Changeset:
    return Changeset(
        changeset_id=row["changeset_id"],
        project_id=row["project_id"],
        run_id=row["run_id"],
        task=TaskSpec(**loads_jsonb(row["task"])),
        status=ChangesetStatus(row["status"]),
        base_branch=row["base_branch"],
        branch=row["branch"],
        pr_url=row["pr_url"],
        pr_number=row["pr_number"],
        head_sha=_optional_column(row, "head_sha"),
        github_pr_status=_optional_column(row, "github_pr_status"),
        external_ci_status=_optional_column(row, "external_ci_status"),
        external_ci_awaiting_since=_optional_column(
            row, "external_ci_awaiting_since"
        ),
        ci_retry_count=_optional_column(row, "ci_retry_count") or 0,
        ci_remediation_status=(
            _optional_column(row, "ci_remediation_status") or CIRemediationStatus.idle
        ),
        ci_failure_key=_optional_column(row, "ci_failure_key"),
        ci_failure_summary=_optional_column(row, "ci_failure_summary"),
        merge_sha=row["merge_sha"],
        diff_stat=loads_jsonb(row["diff_stat"]),
        prompts=_prompts_from_row(row),
        contract_bundle=_contract_bundle_from_row(row),
        requirement_ledger=_requirement_ledger_from_row(row),
        inspection_snapshot=_inspection_snapshot_from_row(row),
        dependency_slice=_dependency_slice_from_row(row),
        verification_plan=_verification_plan_from_row(row),
        verification_coverage=_verification_coverage_from_row(row),
        runtime_acceptance_plan=_runtime_acceptance_plan_from_row(row),
        runtime_evidence_assessment=_runtime_evidence_assessment_from_row(row),
        review_verdict=_review_verdict_from_row(row),
        publication_authorization=_publication_authorization_from_row(row),
        tenant_policy_snapshot=_tenant_policy_snapshot_from_row(row),
        effective_safety_policy_sha256=_optional_column(
            row, "effective_safety_policy_sha256"
        ),
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _assert_idempotency_request(row: asyncpg.Record, request_sha256: str) -> None:
    if str(row["idempotency_request_sha256"]) != request_sha256:
        raise ChangesetIdempotencyConflict(
            "Idempotency key is already bound to a different canonical request payload"
        )


async def get_idempotent_changeset(
    pool: asyncpg.Pool,
    *,
    project_id: str,
    idempotency_key: str,
    idempotency_request_sha256: str,
) -> Changeset | None:
    """Resolve an accepted request before mutable creation preconditions."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM codegen_changesets
            WHERE project_id = $1 AND idempotency_key = $2
            """,
            project_id,
            idempotency_key,
        )
    if row is None:
        return None
    _assert_idempotency_request(row, idempotency_request_sha256)
    return _row_to_changeset(row)


async def get_idempotent_retry_changeset(
    pool: asyncpg.Pool,
    *,
    project_id: str,
    retry_of_changeset_id: str,
    idempotency_key: str,
    idempotency_request_sha256: str,
) -> Changeset | None:
    """Resolve the canonical same-tenant retry child before mutable policy."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM codegen_changesets
            WHERE project_id = $1 AND idempotency_key = $2
            """,
            project_id,
            idempotency_key,
        )
    if row is None:
        return None
    if str(row["retry_of_changeset_id"] or "") != retry_of_changeset_id:
        raise ChangesetIdempotencyConflict(
            "Idempotency key is already bound to a different retry lineage"
        )
    _assert_idempotency_request(row, idempotency_request_sha256)
    return _row_to_changeset(row)


async def create_changeset(
    pool: asyncpg.Pool,
    *,
    changeset_id: str,
    project_id: str,
    idempotency_key: str,
    idempotency_request_sha256: str,
    run_id: str | None,
    base_branch: str | None,
    task: dict[str, Any],
    repository_target: RepositoryTarget,
    tenant_policy_snapshot: TenantCodegenConnectionPolicy | None = None,
    effective_safety_policy_sha256: str | None = None,
) -> tuple[Changeset, bool]:
    """Atomically create or return one immutable request for a tenant key.

    The unique ``(project_id, idempotency_key)`` database boundary serializes
    concurrent callers. Only the caller whose insert succeeds receives
    ``created=True`` and may enqueue the downstream job.
    """
    if repository_target.project_id != project_id:
        raise ValueError("Changeset project does not match its repository grant")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO codegen_changesets
                    (changeset_id, project_id, idempotency_key,
                     idempotency_request_sha256, run_id, status, base_branch,
                     task, tenant_policy_snapshot,
                     effective_safety_policy_sha256, repository_grant_id,
                     repository_id, repository_installation_id,
                     repository_full_name, repository_target_quarantined)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb,
                        $10, $11, $12, $13, $14, false)
                ON CONFLICT (project_id, idempotency_key) DO NOTHING
                RETURNING *
                """,
                changeset_id,
                project_id,
                idempotency_key,
                idempotency_request_sha256,
                run_id,
                ChangesetStatus.queued.value,
                base_branch,
                json.dumps(task),
                (
                    tenant_policy_snapshot.model_dump_json()
                    if tenant_policy_snapshot is not None
                    else None
                ),
                effective_safety_policy_sha256,
                repository_target.grant_id,
                repository_target.repository_id,
                repository_target.installation_id,
                repository_target.repository_full_name,
            )
            created = row is not None
            if row is None:
                row = await conn.fetchrow(
                    """
                    SELECT *
                    FROM codegen_changesets
                    WHERE project_id = $1 AND idempotency_key = $2
                    FOR UPDATE
                    """,
                    project_id,
                    idempotency_key,
                )
                if row is None:
                    raise RuntimeError(
                        "Changeset idempotency conflict did not resolve to an "
                        "existing row"
                    )
                _assert_idempotency_request(row, idempotency_request_sha256)
    return _row_to_changeset(row), created


async def create_retry_changeset(
    pool: asyncpg.Pool,
    *,
    changeset_id: str,
    retry_of_changeset_id: str,
    project_id: str,
    idempotency_key: str,
    idempotency_request_sha256: str,
    run_id: str | None,
    base_branch: str | None,
    task: dict[str, Any],
    repository_target: RepositoryTarget,
    tenant_policy_snapshot: TenantCodegenConnectionPolicy | None = None,
    effective_safety_policy_sha256: str | None = None,
) -> tuple[Changeset, bool]:
    """Create or return the one canonical child for a retryable changeset.

    ``codegen_changesets_one_retry_child_idx`` is the cross-replica
    idempotency boundary. Only the request that inserts the row may enqueue
    it; duplicate or concurrent requests receive the already-created child.
    """
    if repository_target.project_id != project_id:
        raise ValueError("Changeset project does not match its repository grant")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO codegen_changesets
                    (changeset_id, project_id, idempotency_key,
                     idempotency_request_sha256, run_id, status, base_branch,
                     task, tenant_policy_snapshot, effective_safety_policy_sha256,
                     repository_grant_id, repository_id,
                     repository_installation_id, repository_full_name,
                     repository_target_quarantined, retry_of_changeset_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb,
                        $10, $11, $12, $13, $14, false, $15)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                changeset_id,
                project_id,
                idempotency_key,
                idempotency_request_sha256,
                run_id,
                ChangesetStatus.queued.value,
                base_branch,
                json.dumps(task),
                (
                    tenant_policy_snapshot.model_dump_json()
                    if tenant_policy_snapshot is not None
                    else None
                ),
                effective_safety_policy_sha256,
                repository_target.grant_id,
                repository_target.repository_id,
                repository_target.installation_id,
                repository_target.repository_full_name,
                retry_of_changeset_id,
            )
            if row is not None:
                return _row_to_changeset(row), True

            lineage_row = await conn.fetchrow(
                """
                SELECT *
                FROM codegen_changesets
                WHERE project_id = $1 AND retry_of_changeset_id = $2
                FOR UPDATE
                """,
                project_id,
                retry_of_changeset_id,
            )
            key_row = await conn.fetchrow(
                """
                SELECT *
                FROM codegen_changesets
                WHERE project_id = $1 AND idempotency_key = $2
                FOR UPDATE
                """,
                project_id,
                idempotency_key,
            )
            if lineage_row is not None:
                if str(lineage_row["idempotency_key"]) != idempotency_key:
                    raise ChangesetIdempotencyConflict(
                        "Retry lineage is already bound to a different idempotency key"
                    )
                _assert_idempotency_request(
                    lineage_row, idempotency_request_sha256
                )
                return _row_to_changeset(lineage_row), False
            if key_row is not None:
                if str(key_row["retry_of_changeset_id"] or "") != retry_of_changeset_id:
                    raise ChangesetIdempotencyConflict(
                        "Idempotency key is already bound to a different retry lineage"
                    )
                _assert_idempotency_request(key_row, idempotency_request_sha256)
                return _row_to_changeset(key_row), False

            foreign_project = await conn.fetchval(
                """
                SELECT project_id
                FROM codegen_changesets
                WHERE retry_of_changeset_id = $1
                LIMIT 1
                """,
                retry_of_changeset_id,
            )
            if foreign_project is not None:
                raise ChangesetIdempotencyConflict(
                    "Retry lineage is bound to a changeset in another project"
                )
            raise RuntimeError(
                "Retry idempotency conflict did not resolve to an existing child"
            )


async def set_safety_policy_provenance(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    tenant_policy_snapshot: TenantCodegenConnectionPolicy,
    effective_safety_policy_sha256: str,
) -> Changeset | None:
    """Persist the immutable tenant snapshot and latest effective-policy digest.

    A legacy row may not have a tenant snapshot yet, so the first safe execution
    fills it. Once present it is never replaced by a later connection update.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE codegen_changesets
            SET tenant_policy_snapshot = COALESCE(
                    tenant_policy_snapshot, $2::jsonb
                ),
                effective_safety_policy_sha256 = $3,
                updated_at = now()
            WHERE changeset_id = $1
            RETURNING *
            """,
            changeset_id,
            tenant_policy_snapshot.model_dump_json(),
            effective_safety_policy_sha256,
        )
    return _row_to_changeset(row) if row else None


async def get_changeset(pool: asyncpg.Pool, changeset_id: str) -> Changeset | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM codegen_changesets WHERE changeset_id = $1",
            changeset_id,
        )
    return _row_to_changeset(row) if row else None


async def get_changeset_by_head_sha(
    pool: asyncpg.Pool,
    head_sha: str,
    repository_id: int,
    installation_id: int,
) -> Changeset | None:
    """Find open work by exact head and immutable GitHub repository identity."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT cs.* FROM codegen_changesets cs
            WHERE cs.head_sha = $1
              AND cs.repository_id = $2
              AND cs.repository_installation_id = $3
              AND NOT cs.repository_target_quarantined
              AND cs.status = 'pr_open'
            ORDER BY cs.created_at DESC
            LIMIT 1
            """,
            head_sha,
            repository_id,
            installation_id,
        )
    return _row_to_changeset(row) if row else None


async def get_changeset_by_pr_number(
    pool: asyncpg.Pool,
    pr_number: int,
    repository_id: int,
    installation_id: int,
) -> Changeset | None:
    """Find work by PR number and immutable GitHub repository identity."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT cs.* FROM codegen_changesets cs
            WHERE cs.pr_number = $1
              AND cs.repository_id = $2
              AND cs.repository_installation_id = $3
              AND NOT cs.repository_target_quarantined
            ORDER BY cs.created_at DESC
            LIMIT 1
            """,
            pr_number,
            repository_id,
            installation_id,
        )
    return _row_to_changeset(row) if row else None


async def list_changesets(
    pool: asyncpg.Pool, project_id: str, limit: int = 50
) -> list[Changeset]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM codegen_changesets
            WHERE project_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            project_id,
            limit,
        )
    return [_row_to_changeset(r) for r in rows]


async def list_syncable_changeset_ids(pool: asyncpg.Pool) -> list[str]:
    """Open PR changesets to recover from GitHub; never age them out."""
    statuses = [s.value for s in CI_SYNCABLE_STATUSES]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT changeset_id FROM codegen_changesets
            WHERE status = ANY($1::text[])
              AND (github_pr_status IS NULL OR github_pr_status IN ('open', 'draft'))
            ORDER BY updated_at ASC, changeset_id ASC
            """,
            statuses,
        )
    return [r["changeset_id"] for r in rows]


async def _guarded_update(
    pool: asyncpg.Pool,
    changeset_id: str,
    target: ChangesetStatus,
    *,
    set_clause: str,
    params: tuple[Any, ...],
) -> Changeset | None:
    """Row-locked, state-machine-checked status update.

    The single place the ``SELECT … FOR UPDATE`` → :func:`assert_transition` →
    ``UPDATE … RETURNING`` dance lives, so concurrent updates can't corrupt the
    status. ``set_clause`` adds columns beyond ``status``/``updated_at`` and is
    composed only from trusted in-module SQL literals (never request data); its
    bind values are ``$3``+ supplied in ``params``. Returns the updated
    changeset, ``None`` if it does not exist, or raises
    :class:`~app.models.changeset.InvalidTransition` for an illegal move.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchval(
                "SELECT status FROM codegen_changesets WHERE changeset_id = $1 FOR UPDATE",
                changeset_id,
            )
            if current is None:
                return None
            assert_transition(ChangesetStatus(current), target)
            row = await conn.fetchrow(
                f"""
                UPDATE codegen_changesets
                SET status = $2, {set_clause}, updated_at = now()
                WHERE changeset_id = $1
                RETURNING *
                """,
                changeset_id,
                target.value,
                *params,
            )
    return _row_to_changeset(row)


async def transition_changeset(
    pool: asyncpg.Pool,
    changeset_id: str,
    target: ChangesetStatus,
    *,
    error: str | None = None,
) -> Changeset | None:
    """Move a changeset to ``target``, enforcing the lifecycle state machine."""
    return await _guarded_update(
        pool,
        changeset_id,
        target,
        set_clause="error = COALESCE($3, error)",
        params=(error,),
    )


async def mark_pr_open(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    branch: str,
    observation: PullRequestObservation,
    external_ci_status: ExternalCIStatus,
    diff_stat: dict[str, Any],
) -> Changeset | None:
    """Atomically journal and project the exact GitHub PR created by APDL."""
    if observation.changeset_id != changeset_id or observation.action != "opened":
        raise ValueError("initial PR observation must identify this changeset and open")
    if observation.status not in {GitHubPRStatus.draft, GitHubPRStatus.open}:
        raise ValueError("initial PR observation must be draft or open")
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchval(
                "SELECT status FROM codegen_changesets WHERE changeset_id = $1 FOR UPDATE",
                changeset_id,
            )
            if current is None:
                return None
            assert_transition(ChangesetStatus(current), ChangesetStatus.pr_open)
            inserted = await conn.fetchval(
                """
                INSERT INTO codegen_pull_request_observations
                    (observation_id, delivery_id, changeset_id, repository,
                     pr_number, head_sha, status, github_updated_at,
                     observed_at, payload)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
                ON CONFLICT DO NOTHING RETURNING observation_id
                """,
                observation.observation_id,
                observation.delivery_id,
                observation.changeset_id,
                observation.repository,
                observation.pr_number,
                observation.head_sha,
                observation.status.value,
                observation.github_updated_at,
                observation.observed_at,
                observation.model_dump_json(),
            )
            if inserted is None:
                raise ValueError("initial pull-request observation already exists")
            row = await conn.fetchrow(
                """
                UPDATE codegen_changesets
                SET status = 'pr_open', branch = $2, pr_url = $3,
                    pr_number = $4, head_sha = $5, github_pr_status = $6,
                    external_ci_status = $7, diff_stat = $8::jsonb,
                    external_ci_awaiting_since = now(),
                    ci_remediation_status = 'idle', updated_at = now()
                WHERE changeset_id = $1 RETURNING *
                """,
                changeset_id,
                branch,
                observation.github_url,
                observation.pr_number,
                observation.head_sha,
                observation.status.value,
                external_ci_status.value,
                json.dumps(diff_stat),
            )
    return _row_to_changeset(row)


async def set_prompts(
    pool: asyncpg.Pool, changeset_id: str, prompts: list[dict[str, Any]]
) -> None:
    """Persist the run's LLM prompt transcript (no status transition).

    Written once per edit attempt — success or failure — so the admin console
    can show exactly what the run sent to the model(s). Deliberately outside
    the state machine: the transcript is diagnostic metadata, valid to record
    in any state.
    """
    bounded = bound_prompt_transcript(prompts)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE codegen_changesets
            SET prompts = $2::jsonb, updated_at = now()
            WHERE changeset_id = $1
            """,
            changeset_id,
            json.dumps(bounded),
        )


async def set_contract_bundle(
    pool: asyncpg.Pool,
    changeset_id: str,
    bundle: ContractBundle,
) -> None:
    """Persist exact dependency evidence without changing lifecycle or CI state."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE codegen_changesets
            SET contract_bundle = $2::jsonb, updated_at = now()
            WHERE changeset_id = $1
            """,
            changeset_id,
            json.dumps(bundle.model_dump(mode="json")),
        )


async def set_requirement_ledger(
    pool: asyncpg.Pool,
    changeset_id: str,
    ledger: RequirementLedger,
) -> None:
    """Persist the stable requirement/evidence mapping without changing CI state."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE codegen_changesets
            SET requirement_ledger = $2::jsonb, updated_at = now()
            WHERE changeset_id = $1
            """,
            changeset_id,
            json.dumps(ledger.model_dump(mode="json")),
        )


async def set_inspection_evidence(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    snapshot: InspectionSnapshot | None,
    dependency_slice: DependencySlice | None,
) -> None:
    """Persist auditable repository evidence without changing lifecycle or CI."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE codegen_changesets
            SET inspection_snapshot = COALESCE($2::jsonb, inspection_snapshot),
                dependency_slice = COALESCE($3::jsonb, dependency_slice),
                updated_at = now()
            WHERE changeset_id = $1
            """,
            changeset_id,
            (
                json.dumps(snapshot.model_dump(mode="json"))
                if snapshot is not None
                else None
            ),
            (
                json.dumps(dependency_slice.model_dump(mode="json"))
                if dependency_slice is not None
                else None
            ),
        )


async def set_verification_evidence(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    plan: VerificationPlan | None,
    coverage: VerificationCoverage | None,
) -> None:
    """Persist expected GitHub coverage facts, never a CI result."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE codegen_changesets
            SET verification_plan = COALESCE($2::jsonb, verification_plan),
                verification_coverage = COALESCE($3::jsonb, verification_coverage),
                updated_at = now()
            WHERE changeset_id = $1
            """,
            changeset_id,
            json.dumps(plan.model_dump(mode="json")) if plan is not None else None,
            (
                json.dumps(coverage.model_dump(mode="json"))
                if coverage is not None
                else None
            ),
        )


async def set_review_verdict(
    pool: asyncpg.Pool,
    changeset_id: str,
    verdict: ReviewVerdict,
) -> None:
    """Persist the evidence-backed pre-push judgment without changing CI state."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE codegen_changesets
            SET review_verdict = $2::jsonb, updated_at = now()
            WHERE changeset_id = $1
            """,
            changeset_id,
            json.dumps(verdict.model_dump(mode="json")),
        )


async def set_runtime_acceptance_plan(
    pool: asyncpg.Pool,
    changeset_id: str,
    plan: RuntimeAcceptancePlan,
) -> None:
    """Persist planned GitHub runtime evidence without claiming a result."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE codegen_changesets
            SET runtime_acceptance_plan = $2::jsonb, updated_at = now()
            WHERE changeset_id = $1
            """,
            changeset_id,
            json.dumps(plan.model_dump(mode="json")),
        )


async def set_publication_authorization(
    pool: asyncpg.Pool,
    changeset_id: str,
    authorization: PublicationAuthorizationRecord,
) -> None:
    """Persist exact publication authority before any GitHub credential use."""
    validated = PUBLICATION_AUTHORIZATION_ADAPTER.validate_python(
        authorization.model_dump(mode="python")
    )
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE codegen_changesets
            SET publication_authorization = $2::jsonb, updated_at = now()
            WHERE changeset_id = $1
            """,
            changeset_id,
            json.dumps(validated.model_dump(mode="json")),
        )


async def mark_merged(
    pool: asyncpg.Pool, changeset_id: str, *, merge_sha: str
) -> Changeset | None:
    """Transition to ``merged`` and persist the merge commit SHA.

    The SHA is what a later ``/revert`` reverts deterministically (``git
    revert``) instead of asking the agent to reconstruct the change from prose.
    """
    return await _guarded_update(
        pool,
        changeset_id,
        ChangesetStatus.merged,
        set_clause="merge_sha = $3",
        params=(merge_sha,),
    )


async def list_queued_changeset_ids(pool: asyncpg.Pool) -> list[str]:
    """Ids of changesets still in ``queued``, oldest first.

    Used at startup to re-enqueue work a restart orphaned before it began: a
    queued row has produced nothing (no clone, no branch), so re-running it is
    safe — and the job's queued → cloning claim transition guarantees only one
    worker wins even if a concurrent replica re-enqueues the same rows.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT changeset_id FROM codegen_changesets
            WHERE status = ANY($1::text[])
            ORDER BY created_at ASC
            """,
            [ChangesetStatus.queued.value],
        )
    return [r["changeset_id"] for r in rows]


async def fail_stale_changesets(
    pool: asyncpg.Pool, *, older_than_seconds: int, error: str
) -> list[str]:
    """Fail changesets orphaned mid-pipeline past a deadline; return their ids.

    The job runner uses in-process background tasks, so a process restart leaves
    any changeset in an active (pre-PR, post-claim) state stuck there forever —
    no later step ever runs to advance or fail it. This sweep (run at startup
    and periodically — see ``jobs.runner.run_stale_sweeper``) moves those rows
    to ``error`` so they surface instead of hanging. ``queued`` rows are NOT
    swept: they are re-enqueued at startup instead. The ``older_than_seconds``
    deadline guards against killing work a *concurrent* codegen replica may
    still be running on the shared database: set it longer than any single job
    can take (e.g. ``2 ×`` the job pipeline budget).
    """
    statuses = [s.value for s in _ACTIVE_STATUSES]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE codegen_changesets
            SET status = 'error', error = COALESCE(error, $1), updated_at = now()
            WHERE status = ANY($2::text[])
              AND updated_at < now() - $3 * interval '1 second'
            RETURNING changeset_id
            """,
            error,
            statuses,
            older_than_seconds,
        )
    return [r["changeset_id"] for r in rows]
