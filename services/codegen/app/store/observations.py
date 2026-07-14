"""Append-only persistence for authoritative GitHub observation records.

The tables behind this module are immutable journals.  Inserts use
``ON CONFLICT DO NOTHING`` only; there is intentionally no update/upsert path.
CI poll duplicates are keyed by a stable evidence hash, webhook PR duplicates
by GitHub delivery ID, and remediation claims by the exact failed head and CI
observation that triggered them.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

import asyncpg
from pydantic import BaseModel, ValidationError

from app.models.observations import (
    CIRemediationAttempt,
    CIVerificationObservation,
    CIRemediationStatus,
    ExternalCIStatus,
    GitHubPRStatus,
    PullRequestObservation,
    RemediationDisposition,
)
from app.runtime.models import RuntimeAcceptancePlan

_MAX_LIMIT = 200
_ObservationT = TypeVar("_ObservationT", bound=BaseModel)


class ObservationDecodeError(ValueError):
    """Stored JSON does not satisfy its immutable strict observation schema."""


@dataclass(frozen=True)
class ApplyObservationResult:
    inserted: bool
    projected: bool
    reason: str


@dataclass(frozen=True)
class RepairClaimResult:
    claimed: bool
    attempt_number: int | None
    exhausted: bool
    reason: str


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {_MAX_LIMIT}")
    return value


def _required(value: str, name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{name} cannot be blank")
    return value


def _claim_scope(value: str) -> str:
    value = _required(value, "claim_scope")
    prefix, separator, identity = value.partition(":")
    if separator != ":" or not identity:
        raise ValueError("claim_scope must be a canonical CI signal or check-suite ID")
    if prefix in {"check_suite", "check_run"}:
        if not identity.isdecimal() or int(identity) < 1:
            raise ValueError("check claim scopes require a positive integer ID")
    elif prefix != "commit_status":
        raise ValueError("unsupported remediation claim scope")
    return value


def _decode(row: asyncpg.Record, model: type[_ObservationT]) -> _ObservationT:
    value = row["payload"]
    if isinstance(value, model):
        return value
    if isinstance(value, str):
        raw = value
    else:
        try:
            raw = json.dumps(value, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ObservationDecodeError(
                f"Stored {model.__name__} payload is not JSON."
            ) from exc
    try:
        # Strict models accept their enum/datetime wire representations only on
        # Pydantic's JSON path; dict validation would demand pre-built objects.
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise ObservationDecodeError(
            f"Stored {model.__name__} payload violates its strict schema: {exc}"
        ) from exc


async def insert_pull_request_observation(
    pool: asyncpg.Pool, observation: PullRequestObservation
) -> bool:
    """Append one PR observation; duplicate ID or delivery is a no-op."""
    async with pool.acquire() as conn:
        inserted = await conn.fetchval(
            """
            INSERT INTO codegen_pull_request_observations
                (observation_id, delivery_id, changeset_id, repository, pr_number,
                 head_sha, status, github_updated_at, observed_at, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
            ON CONFLICT DO NOTHING
            RETURNING observation_id
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
    return inserted is not None


async def list_pull_request_observations(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    head_sha: str | None = None,
    limit: int = 50,
) -> list[PullRequestObservation]:
    """Newest PR observations, optionally restricted to one exact head."""
    changeset_id = _required(changeset_id, "changeset_id")
    head_sha = _required(head_sha, "head_sha") if head_sha is not None else None
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT payload FROM codegen_pull_request_observations
            WHERE changeset_id = $1
              AND ($2::text IS NULL OR head_sha = $2)
            ORDER BY github_updated_at DESC, observed_at DESC, observation_id DESC
            LIMIT $3
            """,
            changeset_id,
            head_sha,
            _limit(limit),
        )
    return [_decode(row, PullRequestObservation) for row in rows]


async def latest_pull_request_observation(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    head_sha: str | None = None,
) -> PullRequestObservation | None:
    values = await list_pull_request_observations(
        pool, changeset_id, head_sha=head_sha, limit=1
    )
    return values[0] if values else None


async def insert_ci_verification_observation(
    pool: asyncpg.Pool, observation: CIVerificationObservation
) -> bool:
    """Append CI evidence, deduplicated by exact head and stable evidence hash."""
    async with pool.acquire() as conn:
        inserted = await conn.fetchval(
            """
            INSERT INTO codegen_ci_verification_observations
                (observation_id, changeset_id, repository, pr_number, head_sha,
                 status, evidence_hash, observed_at, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            ON CONFLICT DO NOTHING
            RETURNING observation_id
            """,
            observation.observation_id,
            observation.changeset_id,
            observation.repository,
            observation.pr_number,
            observation.head_sha,
            observation.status.value,
            observation.evidence_hash(),
            observation.observed_at,
            observation.model_dump_json(),
        )
    return inserted is not None


async def list_ci_verification_observations(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    head_sha: str | None = None,
    limit: int = 50,
) -> list[CIVerificationObservation]:
    """Newest CI observations for exactly one PR head; never mix repair heads."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT payload FROM codegen_ci_verification_observations
            WHERE changeset_id = $1
              AND ($2::text IS NULL OR head_sha = $2)
            ORDER BY observed_at DESC, observation_id DESC
            LIMIT $3
            """,
            _required(changeset_id, "changeset_id"),
            _required(head_sha, "head_sha") if head_sha is not None else None,
            _limit(limit),
        )
    return [_decode(row, CIVerificationObservation) for row in rows]


async def latest_ci_verification_observation(
    pool: asyncpg.Pool, changeset_id: str, *, head_sha: str
) -> CIVerificationObservation | None:
    values = await list_ci_verification_observations(
        pool, changeset_id, head_sha=head_sha, limit=1
    )
    return values[0] if values else None


async def apply_pull_request_observation(
    pool: asyncpg.Pool, observation: PullRequestObservation
) -> ApplyObservationResult:
    """Append a PR fact and atomically project only the newest GitHub state."""
    async with pool.acquire() as conn:
        async with conn.transaction():
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
                return ApplyObservationResult(False, False, "duplicate")
            latest = await conn.fetchval(
                """
                SELECT observation_id FROM codegen_pull_request_observations
                WHERE changeset_id = $1
                ORDER BY github_updated_at DESC, observed_at DESC,
                         observation_id DESC LIMIT 1
                """,
                observation.changeset_id,
            )
            if latest != observation.observation_id:
                return ApplyObservationResult(True, False, "superseded_observation")
            row = await conn.fetchrow(
                """
                SELECT cs.*, conn.repo AS connected_repository
                FROM codegen_changesets cs
                JOIN codegen_connections conn ON conn.project_id = cs.project_id
                WHERE cs.changeset_id = $1 FOR UPDATE OF cs
                """,
                observation.changeset_id,
            )
            if row is None:
                return ApplyObservationResult(True, False, "changeset_missing")
            if (
                row["connected_repository"] != observation.repository
                or row["pr_number"] != observation.pr_number
            ):
                return ApplyObservationResult(True, False, "identity_mismatch")

            current_status = row["status"]
            if observation.status is GitHubPRStatus.merged:
                lifecycle = "merged"
            elif observation.status is GitHubPRStatus.closed:
                lifecycle = "abandoned"
            elif current_status == "abandoned" and observation.action in {
                "reopened",
                "polled",
            }:
                lifecycle = "pr_open"
            elif current_status == "pr_open":
                lifecycle = "pr_open"
            else:
                return ApplyObservationResult(True, False, "lifecycle_ineligible")

            head_changed = row["head_sha"] != observation.head_sha
            await conn.fetchrow(
                """
                UPDATE codegen_changesets
                SET status = $2, head_sha = $3, github_pr_status = $4,
                    merge_sha = CASE WHEN $4 = 'merged' THEN $5 ELSE merge_sha END,
                    external_ci_status = CASE
                        WHEN $6 THEN 'pending' ELSE external_ci_status END,
                    external_ci_awaiting_since = CASE
                        WHEN $6 THEN now() ELSE external_ci_awaiting_since END,
                    ci_remediation_status = CASE
                        WHEN $6 THEN 'idle' ELSE ci_remediation_status END,
                    ci_failure_key = CASE WHEN $6 THEN NULL ELSE ci_failure_key END,
                    ci_failure_summary = CASE
                        WHEN $6 THEN NULL ELSE ci_failure_summary END,
                    runtime_evidence_assessment = CASE
                        WHEN $6 THEN NULL ELSE runtime_evidence_assessment END,
                    updated_at = now()
                WHERE changeset_id = $1 RETURNING changeset_id
                """,
                observation.changeset_id,
                lifecycle,
                observation.head_sha,
                observation.status.value,
                observation.merge_sha,
                head_changed,
            )
            if head_changed and row["head_sha"]:
                attempt_row = await conn.fetchrow(
                    """
                    SELECT payload FROM codegen_ci_remediation_attempts
                    WHERE changeset_id = $1
                      AND payload->>'resulting_commit_sha' = $2
                      AND payload->>'disposition' = 'awaiting_ci'
                    ORDER BY recorded_at DESC, event_sequence DESC LIMIT 1
                    """,
                    observation.changeset_id,
                    row["head_sha"],
                )
                if attempt_row is not None:
                    prior = _decode(attempt_row, CIRemediationAttempt)
                    payload = prior.model_dump(mode="json")
                    payload.update(
                        event_sequence=prior.event_sequence + 1,
                        event_id=f"{prior.attempt_id}:{prior.event_sequence + 1}",
                        disposition=RemediationDisposition.superseded.value,
                        recorded_at=observation.observed_at,
                        finished_at=observation.observed_at,
                        error="A newer GitHub PR head superseded this repair.",
                    )
                    superseded = CIRemediationAttempt.model_validate_json(
                        json.dumps(payload)
                    )
                    await conn.fetchval(
                        """
                        INSERT INTO codegen_ci_remediation_attempts
                            (event_id, attempt_id, event_sequence, changeset_id,
                             repository, pr_number, failed_head_sha,
                             failure_observation_id, attempt_number, started_at,
                             recorded_at, payload)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                                $12::jsonb)
                        ON CONFLICT DO NOTHING RETURNING event_id
                        """,
                        superseded.event_id,
                        superseded.attempt_id,
                        superseded.event_sequence,
                        superseded.changeset_id,
                        superseded.repository,
                        superseded.pr_number,
                        superseded.failed_head_sha,
                        superseded.failure_observation_id,
                        superseded.attempt_number,
                        superseded.started_at,
                        superseded.recorded_at,
                        superseded.model_dump_json(),
                    )
    return ApplyObservationResult(True, True, "projected")


async def apply_ci_verification_observation(
    pool: asyncpg.Pool, observation: CIVerificationObservation
) -> ApplyObservationResult:
    """Append exact-head CI evidence and project it without changing lifecycle."""
    evidence_hash = observation.evidence_hash()
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchval(
                """
                INSERT INTO codegen_ci_verification_observations
                    (observation_id, changeset_id, repository, pr_number, head_sha,
                     status, evidence_hash, observed_at, payload)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                ON CONFLICT DO NOTHING RETURNING observation_id
                """,
                observation.observation_id,
                observation.changeset_id,
                observation.repository,
                observation.pr_number,
                observation.head_sha,
                observation.status.value,
                evidence_hash,
                observation.observed_at,
                observation.model_dump_json(),
            )
            if inserted is None:
                return ApplyObservationResult(False, False, "duplicate")
            latest = await conn.fetchval(
                """
                SELECT observation_id FROM codegen_ci_verification_observations
                WHERE changeset_id = $1 AND head_sha = $2
                ORDER BY observed_at DESC, observation_id DESC LIMIT 1
                """,
                observation.changeset_id,
                observation.head_sha,
            )
            if latest != observation.observation_id:
                return ApplyObservationResult(True, False, "superseded_observation")
            row = await conn.fetchrow(
                """
                SELECT cs.*, conn.repo AS connected_repository
                FROM codegen_changesets cs
                JOIN codegen_connections conn ON conn.project_id = cs.project_id
                WHERE cs.changeset_id = $1 FOR UPDATE OF cs
                """,
                observation.changeset_id,
            )
            if row is None:
                return ApplyObservationResult(True, False, "changeset_missing")
            if (
                row["connected_repository"] != observation.repository
                or row["pr_number"] != observation.pr_number
                or row["head_sha"] != observation.head_sha
                or row["status"] != "pr_open"
                or row["github_pr_status"] not in {"open", "draft"}
            ):
                return ApplyObservationResult(True, False, "stale_or_ineligible_head")

            remediation = row["ci_remediation_status"]
            if observation.status is ExternalCIStatus.passed and remediation in {
                CIRemediationStatus.awaiting_ci.value,
                CIRemediationStatus.repairing.value,
                CIRemediationStatus.diagnosing.value,
            }:
                remediation = CIRemediationStatus.resolved.value
            elif observation.status is ExternalCIStatus.unverified_external_ci:
                remediation = CIRemediationStatus.idle.value
            await conn.fetchrow(
                """
                UPDATE codegen_changesets
                SET external_ci_status = $2, ci_remediation_status = $3,
                    ci_failure_key = $4, ci_failure_summary = $5,
                    runtime_evidence_assessment = NULL,
                    updated_at = now()
                WHERE changeset_id = $1 AND head_sha = $6
                RETURNING changeset_id
                """,
                observation.changeset_id,
                observation.status.value,
                remediation,
                observation.failure_key,
                observation.failure_summary,
                observation.head_sha,
            )
            if observation.status is ExternalCIStatus.passed:
                attempt_row = await conn.fetchrow(
                    """
                    SELECT payload FROM codegen_ci_remediation_attempts
                    WHERE changeset_id = $1
                      AND payload->>'resulting_commit_sha' = $2
                      AND payload->>'disposition' = 'awaiting_ci'
                    ORDER BY recorded_at DESC, event_sequence DESC LIMIT 1
                    """,
                    observation.changeset_id,
                    observation.head_sha,
                )
                if attempt_row is not None:
                    prior = _decode(attempt_row, CIRemediationAttempt)
                    payload = prior.model_dump(mode="json")
                    payload.update(
                        event_sequence=prior.event_sequence + 1,
                        event_id=f"{prior.attempt_id}:{prior.event_sequence + 1}",
                        disposition=RemediationDisposition.repaired.value,
                        recorded_at=observation.observed_at,
                        finished_at=observation.observed_at,
                        error=None,
                    )
                    repaired = CIRemediationAttempt.model_validate_json(
                        json.dumps(payload)
                    )
                    await conn.fetchval(
                        """
                        INSERT INTO codegen_ci_remediation_attempts
                            (event_id, attempt_id, event_sequence, changeset_id,
                             repository, pr_number, failed_head_sha,
                             failure_observation_id, attempt_number, started_at,
                             recorded_at, payload)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                                $12::jsonb)
                        ON CONFLICT DO NOTHING RETURNING event_id
                        """,
                        repaired.event_id,
                        repaired.attempt_id,
                        repaired.event_sequence,
                        repaired.changeset_id,
                        repaired.repository,
                        repaired.pr_number,
                        repaired.failed_head_sha,
                        repaired.failure_observation_id,
                        repaired.attempt_number,
                        repaired.started_at,
                        repaired.recorded_at,
                        repaired.model_dump_json(),
                    )
    return ApplyObservationResult(True, True, "projected")


async def insert_ci_remediation_attempt(
    pool: asyncpg.Pool, attempt: CIRemediationAttempt
) -> bool:
    """Append one immutable remediation event; never overwrite an earlier event."""
    async with pool.acquire() as conn:
        inserted = await conn.fetchval(
            """
            INSERT INTO codegen_ci_remediation_attempts
                (event_id, attempt_id, event_sequence, changeset_id, repository,
                 pr_number, failed_head_sha, failure_observation_id,
                 attempt_number, started_at, recorded_at, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
            ON CONFLICT DO NOTHING
            RETURNING event_id
            """,
            attempt.event_id,
            attempt.attempt_id,
            attempt.event_sequence,
            attempt.changeset_id,
            attempt.repository,
            attempt.pr_number,
            attempt.failed_head_sha,
            attempt.failure_observation_id,
            attempt.attempt_number,
            attempt.started_at,
            attempt.recorded_at,
            attempt.model_dump_json(),
        )
    return inserted is not None


async def list_ci_remediation_attempts(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    failed_head_sha: str | None = None,
    limit: int = 50,
) -> list[CIRemediationAttempt]:
    """Newest immutable remediation events for one exact failed head."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT payload FROM codegen_ci_remediation_attempts
            WHERE changeset_id = $1
              AND ($2::text IS NULL OR failed_head_sha = $2)
            ORDER BY recorded_at DESC, attempt_number DESC,
                     event_sequence DESC, event_id DESC
            LIMIT $3
            """,
            _required(changeset_id, "changeset_id"),
            (
                _required(failed_head_sha, "failed_head_sha")
                if failed_head_sha is not None
                else None
            ),
            _limit(limit),
        )
    return [_decode(row, CIRemediationAttempt) for row in rows]


async def latest_ci_remediation_attempt(
    pool: asyncpg.Pool, changeset_id: str, *, failed_head_sha: str
) -> CIRemediationAttempt | None:
    values = await list_ci_remediation_attempts(
        pool, changeset_id, failed_head_sha=failed_head_sha, limit=1
    )
    return values[0] if values else None


async def claim_failed_ci_observation(
    pool: asyncpg.Pool,
    observation: CIVerificationObservation,
    *,
    claim_scope: str,
    max_attempts: int,
    budget_seconds: int,
) -> RepairClaimResult:
    """Claim one bounded remediation for the current exact failed PR head."""
    if observation.status is not ExternalCIStatus.failed:
        raise ValueError("only failed CI observations can be remediated")
    claim_scope = _claim_scope(claim_scope)
    if claim_scope not in observation.remediation_claim_scopes():
        raise ValueError("claim_scope is not present in the failed observation")
    if max_attempts < 0 or budget_seconds < 0:
        raise ValueError("repair limits cannot be negative")

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT cs.*, conn.repo AS connected_repository
                FROM codegen_changesets cs
                JOIN codegen_connections conn ON conn.project_id = cs.project_id
                WHERE cs.changeset_id = $1 FOR UPDATE OF cs
                """,
                observation.changeset_id,
            )
            if row is None:
                return RepairClaimResult(False, None, False, "changeset_missing")
            if (
                row["connected_repository"] != observation.repository
                or row["pr_number"] != observation.pr_number
                or row["head_sha"] != observation.head_sha
                or row["status"] != "pr_open"
                or row["github_pr_status"] not in {"open", "draft"}
                or row["external_ci_status"] != "failed"
            ):
                return RepairClaimResult(False, None, False, "stale_or_ineligible_head")
            retry_count = int(row["ci_retry_count"] or 0)
            exhausted = retry_count >= max_attempts
            if budget_seconds > 0:
                repair_started_at = await conn.fetchval(
                    """
                    SELECT COALESCE(MIN(started_at), $2)
                    FROM codegen_ci_remediation_attempts
                    WHERE changeset_id = $1
                    """,
                    observation.changeset_id,
                    observation.observed_at,
                )
                budget_expired = await conn.fetchval(
                    "SELECT now() > $1 + $2 * interval '1 second'",
                    repair_started_at,
                    budget_seconds,
                )
                exhausted = exhausted or bool(budget_expired)
            if exhausted:
                await conn.fetchrow(
                    """
                    UPDATE codegen_changesets
                    SET ci_remediation_status = 'exhausted', updated_at = now()
                    WHERE changeset_id = $1 RETURNING changeset_id
                    """,
                    observation.changeset_id,
                )
                return RepairClaimResult(False, None, True, "budget_exhausted")

            claimed = await conn.fetchval(
                """
                INSERT INTO codegen_ci_remediation_claims
                    (changeset_id, failed_head_sha, claim_scope,
                     failure_observation_id, claimed_at)
                SELECT $1, $2, $3, $4, now()
                WHERE EXISTS (
                    SELECT 1
                    FROM codegen_ci_verification_observations ci,
                         LATERAL jsonb_array_elements(ci.payload->'signals') signal
                    WHERE ci.observation_id = $4
                      AND ci.changeset_id = $1
                      AND ci.head_sha = $2
                      AND ci.status = 'failed'
                      AND signal->>'conclusion' = 'failed'
                      AND (
                          ($3 LIKE 'check_suite:%'
                           AND signal->>'check_suite_id' = split_part($3, ':', 2))
                          OR
                          ($3 NOT LIKE 'check_suite:%'
                           AND signal->>'signal_id' = $3)
                      )
                )
                  AND $4 = (
                    SELECT ci.observation_id
                    FROM codegen_ci_verification_observations ci
                    WHERE ci.changeset_id = $1 AND ci.head_sha = $2
                    ORDER BY ci.observed_at DESC, ci.observation_id DESC
                    LIMIT 1
                  )
                ON CONFLICT (changeset_id, failed_head_sha, claim_scope)
                DO NOTHING RETURNING changeset_id
                """,
                observation.changeset_id,
                observation.head_sha,
                claim_scope,
                observation.observation_id,
            )
            if claimed is None:
                return RepairClaimResult(False, None, False, "duplicate_claim")
            attempt_number = retry_count + 1
            await conn.fetchrow(
                """
                UPDATE codegen_changesets
                SET ci_retry_count = $2, ci_remediation_status = 'diagnosing',
                    ci_failure_key = $3, ci_failure_summary = $4,
                    updated_at = now()
                WHERE changeset_id = $1 RETURNING changeset_id
                """,
                observation.changeset_id,
                attempt_number,
                observation.failure_key,
                observation.failure_summary,
            )
    return RepairClaimResult(True, attempt_number, False, "claimed")


async def project_repair_result(
    pool: asyncpg.Pool,
    *,
    changeset_id: str,
    failed_head_sha: str,
    resulting_head_sha: str | None,
    exhausted: bool,
    error: str | None,
    runtime_acceptance_plan: RuntimeAcceptancePlan | None = None,
) -> bool:
    """CAS a repair result so an old-head edit cannot overwrite newer GitHub state."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM codegen_changesets WHERE changeset_id = $1 FOR UPDATE",
                _required(changeset_id, "changeset_id"),
            )
            if (
                row is None
                or row["status"] != "pr_open"
                or row["github_pr_status"] not in {"open", "draft"}
                or row["head_sha"] != _required(failed_head_sha, "failed_head_sha")
            ):
                return False
            if resulting_head_sha:
                await conn.fetchrow(
                    """
                    UPDATE codegen_changesets
                    SET head_sha = $2, external_ci_status = 'pending',
                        external_ci_awaiting_since = now(),
                        ci_remediation_status = 'awaiting_ci',
                        ci_failure_key = NULL, ci_failure_summary = NULL,
                        runtime_evidence_assessment = NULL,
                        runtime_acceptance_plan = COALESCE($3::jsonb, runtime_acceptance_plan),
                        error = NULL, updated_at = now()
                    WHERE changeset_id = $1 RETURNING changeset_id
                    """,
                    changeset_id,
                    _required(resulting_head_sha, "resulting_head_sha"),
                    (
                        runtime_acceptance_plan.model_dump_json()
                        if runtime_acceptance_plan is not None
                        else None
                    ),
                )
            else:
                await conn.fetchrow(
                    """
                    UPDATE codegen_changesets
                    SET ci_remediation_status = $2,
                        error = COALESCE($3, error), updated_at = now()
                    WHERE changeset_id = $1 RETURNING changeset_id
                    """,
                    changeset_id,
                    "exhausted" if exhausted else "idle",
                    error,
                )
    return True


async def set_remediation_in_progress(
    pool: asyncpg.Pool,
    *,
    changeset_id: str,
    failed_head_sha: str,
    status: CIRemediationStatus,
) -> bool:
    """CAS the mutable projection while immutable attempt events retain history."""
    if status not in {
        CIRemediationStatus.diagnosing,
        CIRemediationStatus.repairing,
    }:
        raise ValueError("only in-progress remediation statuses are accepted")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE codegen_changesets
            SET ci_remediation_status = $3, updated_at = now()
            WHERE changeset_id = $1 AND head_sha = $2
              AND status = 'pr_open'
              AND github_pr_status IN ('open', 'draft')
            RETURNING changeset_id
            """,
            changeset_id,
            failed_head_sha,
            status.value,
        )
    return row is not None


def all_observation_models() -> Sequence[type[BaseModel]]:
    """Canonical journal payload types (useful to migration/schema tooling)."""
    return (
        PullRequestObservation,
        CIVerificationObservation,
        CIRemediationAttempt,
    )
