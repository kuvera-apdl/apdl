"""Append-only exact-head runtime evidence and its read-only projection."""

from __future__ import annotations

import json

import asyncpg
from pydantic import ValidationError

from app.runtime.models import RuntimeAcceptancePlan, RuntimeEvidenceObservation
from app.store.observations import ApplyObservationResult

_MAX_LIMIT = 200


class RuntimeEvidenceDecodeError(ValueError):
    """Stored runtime evidence violates its strict canonical schema."""


def _decode(row: asyncpg.Record) -> RuntimeEvidenceObservation:
    payload = row["payload"]
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    try:
        return RuntimeEvidenceObservation.model_validate_json(raw)
    except ValidationError as exc:
        raise RuntimeEvidenceDecodeError(
            f"Stored RuntimeEvidenceObservation violates its strict schema: {exc}"
        ) from exc


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {_MAX_LIMIT}")
    return value


def _runtime_plan_hash(value) -> str | None:
    if value is None:
        return None
    raw = value if isinstance(value, str) else json.dumps(value)
    try:
        return RuntimeAcceptancePlan.model_validate_json(raw).evidence_hash()
    except ValidationError as exc:
        raise RuntimeEvidenceDecodeError(
            "Stored RuntimeAcceptancePlan violates its strict schema."
        ) from exc


async def list_runtime_evidence_observations(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    head_sha: str | None = None,
    ci_observation_id: str | None = None,
    limit: int = 50,
) -> list[RuntimeEvidenceObservation]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT payload FROM codegen_runtime_evidence_observations
            WHERE changeset_id = $1
              AND ($2::text IS NULL OR head_sha = $2)
              AND ($3::text IS NULL OR ci_observation_id = $3)
            ORDER BY observed_at DESC, observation_id DESC
            LIMIT $4
            """,
            changeset_id,
            head_sha,
            ci_observation_id,
            _limit(limit),
        )
    return [_decode(row) for row in rows]


async def latest_runtime_evidence_observation(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    head_sha: str,
    ci_observation_id: str,
) -> RuntimeEvidenceObservation | None:
    values = await list_runtime_evidence_observations(
        pool,
        changeset_id,
        head_sha=head_sha,
        ci_observation_id=ci_observation_id,
        limit=1,
    )
    return values[0] if values else None


async def apply_runtime_evidence_observation(
    pool: asyncpg.Pool,
    observation: RuntimeEvidenceObservation,
) -> ApplyObservationResult:
    """Journal evidence and project only onto the matching live GitHub PR head."""
    evidence_hash = observation.evidence_hash()
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchval(
                """
                INSERT INTO codegen_runtime_evidence_observations
                    (observation_id, changeset_id, repository, pr_number,
                     head_sha, ci_observation_id, evidence_hash, observed_at, payload)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                ON CONFLICT DO NOTHING RETURNING observation_id
                """,
                observation.observation_id,
                observation.changeset_id,
                observation.repository,
                observation.pr_number,
                observation.head_sha,
                observation.ci_observation_id,
                evidence_hash,
                observation.observed_at,
                observation.model_dump_json(),
            )
            if inserted is None:
                return ApplyObservationResult(False, False, "duplicate")
            latest = await conn.fetchval(
                """
                SELECT observation_id FROM codegen_runtime_evidence_observations
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
                SELECT cs.*, cs.repository_full_name AS connected_repository
                FROM codegen_changesets cs
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
                or row["external_ci_status"]
                != observation.assessment.external_ci_status.value
                or _runtime_plan_hash(row["runtime_acceptance_plan"])
                != observation.runtime_acceptance_plan_sha256
            ):
                return ApplyObservationResult(True, False, "stale_or_ineligible_head")
            await conn.fetchrow(
                """
                UPDATE codegen_changesets
                SET runtime_evidence_assessment = $2::jsonb, updated_at = now()
                WHERE changeset_id = $1 AND head_sha = $3
                RETURNING changeset_id
                """,
                observation.changeset_id,
                json.dumps(observation.assessment.model_dump(mode="json")),
                observation.head_sha,
            )
    return ApplyObservationResult(True, True, "projected")


async def claim_runtime_evidence_collection(
    pool: asyncpg.Pool,
    *,
    changeset_id: str,
    head_sha: str,
    ci_observation_id: str,
    lease_seconds: int = 300,
) -> bool:
    """Lease one collection per exact CI observation across service replicas."""
    if lease_seconds <= 0:
        raise ValueError("runtime collection lease must be positive")
    async with pool.acquire() as conn:
        claimed = await conn.fetchval(
            """
            INSERT INTO codegen_runtime_collection_claims
                (changeset_id, head_sha, ci_observation_id, claimed_at)
            SELECT $1, $2, $3, now()
            WHERE NOT EXISTS (
                SELECT 1 FROM codegen_runtime_evidence_observations
                WHERE changeset_id = $1 AND head_sha = $2
                  AND ci_observation_id = $3
            )
            ON CONFLICT (changeset_id, head_sha, ci_observation_id)
            DO UPDATE SET claimed_at = now()
            WHERE codegen_runtime_collection_claims.claimed_at
                  < now() - $4 * interval '1 second'
            RETURNING ci_observation_id
            """,
            changeset_id,
            head_sha,
            ci_observation_id,
            lease_seconds,
        )
    return claimed is not None


async def release_runtime_evidence_collection(
    pool: asyncpg.Pool,
    *,
    changeset_id: str,
    head_sha: str,
    ci_observation_id: str,
) -> None:
    """Release only after an unexpected collection failure so polling can retry."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM codegen_runtime_collection_claims
            WHERE changeset_id = $1 AND head_sha = $2 AND ci_observation_id = $3
            """,
            changeset_id,
            head_sha,
            ci_observation_id,
        )
