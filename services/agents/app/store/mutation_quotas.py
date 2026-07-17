"""Replica-safe, idempotent mutation quota reservations in PostgreSQL."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

import asyncpg


MutationActionType = Literal[
    "create_experiment",
    "update_flag",
    "update_ui_config",
    "feature_proposal",
    "open_pull_request",
]

POLICY_VERSION: Final = "rolling_hour@1"
WINDOW_SECONDS: Final = 60 * 60
ACTION_LIMITS_PER_HOUR: Final[dict[str, int]] = {
    "create_experiment": 5,
    "update_flag": 20,
    "update_ui_config": 30,
    "feature_proposal": 3,
    "open_pull_request": 10,
}

_PROJECT_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_MAX_IDEMPOTENCY_KEY_LENGTH = 256


@dataclass(frozen=True)
class MutationQuotaReservation:
    """Outcome of reserving capacity for one stable mutation identity."""

    project_id: str
    action_type: str
    idempotency_key: str
    policy_version: str
    used: int
    limit: int
    already_reserved: bool


class MutationQuotaExceededError(RuntimeError):
    """The project/action rolling-hour budget has been exhausted."""

    def __init__(self, project_id: str, action_type: str, used: int, limit: int) -> None:
        self.project_id = project_id
        self.action_type = action_type
        self.used = used
        self.limit = limit
        super().__init__(
            f"Mutation quota exceeded for {project_id}/{action_type}: "
            f"{used}/{limit} reservations in the last hour"
        )


class MutationQuotaUnavailableError(RuntimeError):
    """The authoritative quota store could not make a decision."""


def _validate_identity(
    project_id: str, action_type: str, idempotency_key: str
) -> None:
    if not _PROJECT_ID.fullmatch(project_id):
        raise ValueError("project_id must match ^[A-Za-z0-9]{1,64}$")
    if action_type not in ACTION_LIMITS_PER_HOUR:
        raise ValueError(f"Unknown mutation action_type {action_type!r}")
    if (
        not 1 <= len(idempotency_key) <= _MAX_IDEMPOTENCY_KEY_LENGTH
        or not idempotency_key.strip()
    ):
        raise ValueError("idempotency_key must be 1 to 256 characters and not blank")


async def reserve_mutation(
    pool: asyncpg.Pool,
    *,
    project_id: str,
    action_type: MutationActionType,
    idempotency_key: str,
) -> MutationQuotaReservation:
    """Atomically reserve one rolling-hour project/action mutation slot.

    A transaction-scoped advisory lock serializes the count and insert across
    every service replica. Reusing an existing idempotency key always succeeds
    without consuming another slot. Any database error is surfaced as
    :class:`MutationQuotaUnavailableError`; callers must not mutate when the
    authoritative quota store cannot decide.
    """
    _validate_identity(project_id, action_type, idempotency_key)
    limit = ACTION_LIMITS_PER_HOUR[action_type]

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    SELECT pg_advisory_xact_lock(
                        hashtextextended(
                            'apdl:agents:mutation-quota:' || $1 || ':' || $2,
                            0
                        )
                    )
                    """,
                    project_id,
                    action_type,
                )
                existing_policy = await conn.fetchval(
                    """
                    SELECT policy_version
                    FROM agent_mutation_quota_reservations
                    WHERE project_id = $1
                      AND action_type = $2
                      AND idempotency_key = $3
                    """,
                    project_id,
                    action_type,
                    idempotency_key,
                )
                current_count = int(
                    await conn.fetchval(
                        """
                        SELECT count(*)
                        FROM agent_mutation_quota_reservations
                        WHERE project_id = $1
                          AND action_type = $2
                          AND policy_version = $3
                          AND occurred_at >= now() - make_interval(secs => $4)
                        """,
                        project_id,
                        action_type,
                        POLICY_VERSION,
                        WINDOW_SECONDS,
                    )
                    or 0
                )
                if existing_policy is not None:
                    return MutationQuotaReservation(
                        project_id=project_id,
                        action_type=action_type,
                        idempotency_key=idempotency_key,
                        policy_version=str(existing_policy),
                        used=current_count,
                        limit=limit,
                        already_reserved=True,
                    )
                if current_count >= limit:
                    raise MutationQuotaExceededError(
                        project_id, action_type, current_count, limit
                    )

                insert_status = await conn.execute(
                    """
                    INSERT INTO agent_mutation_quota_reservations (
                        project_id, action_type, idempotency_key, policy_version
                    )
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (project_id, action_type, idempotency_key)
                    DO NOTHING
                    """,
                    project_id,
                    action_type,
                    idempotency_key,
                    POLICY_VERSION,
                )
                return MutationQuotaReservation(
                    project_id=project_id,
                    action_type=action_type,
                    idempotency_key=idempotency_key,
                    policy_version=POLICY_VERSION,
                    used=current_count + (1 if insert_status.endswith(" 1") else 0),
                    limit=limit,
                    already_reserved=not insert_status.endswith(" 1"),
                )
    except MutationQuotaExceededError:
        raise
    except Exception as exc:
        raise MutationQuotaUnavailableError(
            f"Mutation quota store unavailable for {project_id}/{action_type}"
        ) from exc
