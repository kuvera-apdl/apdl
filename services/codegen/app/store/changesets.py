"""Persistence for changesets (``codegen_changesets``).

Reads and writes are explicit-column projections (no ``SELECT *`` into the
model). :func:`transition_changeset` enforces the lifecycle state machine inside
a row-locked transaction so concurrent updates cannot corrupt the status.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from app.config import codegen_ci_sync_max_age_seconds
from app.contracts.models import ContractBundle
from app.inspection.models import DependencySlice, InspectionSnapshot
from app.models.changeset import (
    CIRemediationStatus,
    CI_SYNCABLE_STATUSES,
    Changeset,
    ChangesetStatus,
    TaskSpec,
    assert_transition,
)
from app.requirements.models import RequirementLedger
from app.store.jsonb import loads_jsonb

#: Pre-PR pipeline states a running job actively drives (``queued`` excluded:
#: a queued row hasn't started, so a restart re-enqueues it rather than failing
#: it — see :func:`list_queued_changeset_ids`). The job runner uses in-process
#: background tasks, so a process restart orphans any changeset sitting here —
#: :func:`fail_stale_changesets` sweeps the stale ones.
_ACTIVE_STATUSES: tuple[ChangesetStatus, ...] = (
    ChangesetStatus.cloning,
    ChangesetStatus.editing,
    ChangesetStatus.testing,
    ChangesetStatus.pushing,
)


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
        pr_node_id=row["pr_node_id"],
        ci_status=row["ci_status"],
        ci_awaiting_since=_optional_column(row, "ci_awaiting_since"),
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
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create_changeset(
    pool: asyncpg.Pool,
    *,
    changeset_id: str,
    project_id: str,
    run_id: str | None,
    base_branch: str | None,
    task: dict[str, Any],
) -> Changeset:
    """Insert a new changeset in the ``queued`` state."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO codegen_changesets
                (changeset_id, project_id, run_id, status, base_branch, task)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            RETURNING *
            """,
            changeset_id,
            project_id,
            run_id,
            ChangesetStatus.queued.value,
            base_branch,
            json.dumps(task),
        )
    return _row_to_changeset(row)


async def get_changeset(pool: asyncpg.Pool, changeset_id: str) -> Changeset | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM codegen_changesets WHERE changeset_id = $1",
            changeset_id,
        )
    return _row_to_changeset(row) if row else None


async def get_changeset_by_branch(
    pool: asyncpg.Pool, branch: str, repo: str
) -> Changeset | None:
    """Find the active changeset for a ``branch`` on a specific ``repo``.

    Used to route GitHub webhooks. Scoped by repo (joined through the project's
    connection) as well as branch + status, so two connected repos that happen
    to share a branch name can't mis-route each other's CI events.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT cs.* FROM codegen_changesets cs
            JOIN codegen_connections conn ON conn.project_id = cs.project_id
            WHERE cs.branch = $1
              AND conn.repo = $2
              AND cs.status IN (
                  'pr_open', 'ci_running', 'ci_failed', 'ci_passed',
                  'unverified_external_ci'
              )
            ORDER BY cs.created_at DESC
            LIMIT 1
            """,
            branch,
            repo,
        )
    return _row_to_changeset(row) if row else None


async def get_changeset_by_pr_number(
    pool: asyncpg.Pool, pr_number: int, repo: str
) -> Changeset | None:
    """Find the APDL changeset associated with a GitHub pull request."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT cs.* FROM codegen_changesets cs
            JOIN codegen_connections conn ON conn.project_id = cs.project_id
            WHERE cs.pr_number = $1 AND conn.repo = $2
            ORDER BY cs.created_at DESC
            LIMIT 1
            """,
            pr_number,
            repo,
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


async def list_syncable_changeset_ids(
    pool: asyncpg.Pool, *, max_age_seconds: int | None = None
) -> list[str]:
    """Ids of changesets whose CI status a poll can still advance, oldest first.

    The CI poller sweeps these every interval. ``sync_ci_status`` re-checks each
    one under a row lock, so an id that has moved to a terminal/ineligible state
    by the time it runs is simply a no-op.

    ``ci_failed`` is syncable (a re-run can flip it green) but is never abandoned
    automatically, so an age cap (``max_age_seconds``, default from config) drops
    changesets that haven't moved in a long time — otherwise the failed set grows
    unbounded and the poller re-mints a token for each one every interval. Pass
    ``0`` to disable the cap.
    """
    if max_age_seconds is None:
        max_age_seconds = codegen_ci_sync_max_age_seconds()
    statuses = [s.value for s in CI_SYNCABLE_STATUSES]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT changeset_id FROM codegen_changesets
            WHERE status = ANY($1::text[])
              AND ($2 <= 0 OR updated_at >= now() - $2 * interval '1 second')
            ORDER BY updated_at ASC
            """,
            statuses,
            max_age_seconds,
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
    pr_url: str,
    pr_number: int,
    diff_stat: dict[str, Any],
    node_id: str = "",
) -> Changeset | None:
    """Transition ``pushing → pr_open`` and persist the branch + PR identifiers.

    Also stamps ``ci_awaiting_since``: the PR being open is the moment the
    changeset starts awaiting CI, and this anchor (unlike ``updated_at``) is
    never refreshed by later transitions — the CI sync's grace window and
    pending deadline measure from here.
    """
    return await _guarded_update(
        pool,
        changeset_id,
        ChangesetStatus.pr_open,
        set_clause=(
            "branch = $3, pr_url = $4, pr_number = $5, "
            "pr_node_id = $6, diff_stat = $7::jsonb, ci_awaiting_since = now()"
        ),
        params=(branch, pr_url, pr_number, node_id, json.dumps(diff_stat)),
    )


async def set_prompts(
    pool: asyncpg.Pool, changeset_id: str, prompts: list[dict[str, Any]]
) -> None:
    """Persist the run's LLM prompt transcript (no status transition).

    Written once per edit attempt — success or failure — so the admin console
    can show exactly what the run sent to the model(s). Deliberately outside
    the state machine: the transcript is diagnostic metadata, valid to record
    in any state.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE codegen_changesets
            SET prompts = $2::jsonb, updated_at = now()
            WHERE changeset_id = $1
            """,
            changeset_id,
            json.dumps(prompts),
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


async def set_ci_status(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    target: ChangesetStatus,
    ci_status: str,
) -> Changeset | None:
    """Transition to ``target`` and persist the external ``ci_status`` string.

    Used to move ``pr_open → ci_running → ci_passed | ci_failed`` as the repo's
    own CI reports in (via webhook or poll).
    """
    return await _guarded_update(
        pool,
        changeset_id,
        target,
        set_clause="ci_status = $3",
        params=(ci_status,),
    )


async def claim_ci_repair(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    failure_key: str,
    failure_summary: str,
    max_attempts: int,
) -> Changeset | None:
    """Atomically claim one bounded repair for the current failed PR head.

    The failure key includes the GitHub head SHA/check identities. Repeated
    webhook deliveries for the same failure therefore cannot start concurrent
    edits, while a new failed repair commit may consume the next attempt.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM codegen_changesets WHERE changeset_id = $1 FOR UPDATE",
                changeset_id,
            )
            if row is None or row["status"] != ChangesetStatus.ci_failed.value:
                return None
            retry_count = _optional_column(row, "ci_retry_count") or 0
            remediation = _optional_column(row, "ci_remediation_status") or "idle"
            prior_key = _optional_column(row, "ci_failure_key")
            if retry_count >= max_attempts:
                if remediation == CIRemediationStatus.exhausted.value:
                    return None
                row = await conn.fetchrow(
                    """
                    UPDATE codegen_changesets
                    SET ci_remediation_status = 'exhausted', updated_at = now()
                    WHERE changeset_id = $1 RETURNING *
                    """,
                    changeset_id,
                )
                return _row_to_changeset(row)
            if prior_key == failure_key and remediation in ("repairing", "awaiting_ci"):
                return None
            row = await conn.fetchrow(
                """
                UPDATE codegen_changesets
                SET ci_retry_count = ci_retry_count + 1,
                    ci_remediation_status = 'repairing',
                    ci_failure_key = $2,
                    ci_failure_summary = $3,
                    updated_at = now()
                WHERE changeset_id = $1
                RETURNING *
                """,
                changeset_id,
                failure_key,
                failure_summary,
            )
    return _row_to_changeset(row)


async def finish_ci_repair(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    success: bool,
    exhausted: bool = False,
    error: str | None = None,
) -> Changeset | None:
    """Record a repair result; a pushed commit returns to GitHub CI pending."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            current_row = await conn.fetchrow(
                "SELECT * FROM codegen_changesets WHERE changeset_id = $1 FOR UPDATE",
                changeset_id,
            )
            if current_row is None:
                return None
            current = ChangesetStatus(current_row["status"])
            # GitHub may merge/close the PR while an in-flight repair finishes.
            # Never overwrite or annotate that terminal external outcome.
            if current is not ChangesetStatus.ci_failed:
                return _row_to_changeset(current_row)
            if success:
                assert_transition(current, ChangesetStatus.ci_running)
                row = await conn.fetchrow(
                    """
                    UPDATE codegen_changesets
                    SET status = 'ci_running', ci_status = 'pending',
                        ci_remediation_status = 'awaiting_ci', error = NULL,
                        updated_at = now()
                    WHERE changeset_id = $1 RETURNING *
                    """,
                    changeset_id,
                )
            else:
                row = await conn.fetchrow(
                    """
                    UPDATE codegen_changesets
                    SET ci_remediation_status = $2,
                        error = COALESCE($3, error), updated_at = now()
                    WHERE changeset_id = $1 RETURNING *
                    """,
                    changeset_id,
                    "exhausted" if exhausted else "idle",
                    error,
                )
    return _row_to_changeset(row)


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
