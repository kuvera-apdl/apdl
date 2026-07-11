"""Recover GitHub PR and exact-head CI observations without owning either."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import asyncpg

from app.github.checks import GitHubCIEvidence
from app.github.observations import (
    build_ci_verification_observation,
    build_pull_request_observation,
)
from app.models.changeset import ChangesetStatus
from app.models.observations import CIVerificationObservation, GitHubPRStatus
from app.store import changesets as changeset_store
from app.store import connections as connections_store
from app.store.observations import (
    apply_ci_verification_observation,
    apply_pull_request_observation,
)

logger = logging.getLogger(__name__)

PullRequestReader = Callable[[str, int, str], Awaitable[dict[str, Any]]]
CIEvidenceReader = Callable[[str, str, str], Awaitable[GitHubCIEvidence]]
TokenMinter = Callable[[int, str], Awaitable[str]]
CIFailureHandler = Callable[[CIVerificationObservation], Awaitable[None]]


async def sync_github_state(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    get_pull_request: PullRequestReader,
    get_ci_evidence: CIEvidenceReader,
    mint_token: TokenMinter,
    repair_failure: CIFailureHandler | None = None,
    pr_action: str = "polled",
    delivery_id: str | None = None,
) -> CIVerificationObservation | None:
    """Observe the live PR, then journal CI only for its exact current head.

    CI projection never changes ``changeset_status``. A zero-signal GitHub
    response becomes ``unverified_external_ci`` immediately and may be replaced
    by a later exact-head observation if CI subsequently appears.
    """
    changeset = await changeset_store.get_changeset(pool, changeset_id)
    if changeset is None or changeset.pr_number is None:
        return None
    connection = await connections_store.get_connection(pool, changeset.project_id)
    if connection is None:
        return None

    token = await mint_token(connection.installation_id, connection.repo)
    live_pr = await get_pull_request(connection.repo, changeset.pr_number, token)
    now = datetime.now(timezone.utc)
    try:
        pr_observation = build_pull_request_observation(
            changeset_id=changeset_id,
            repository=connection.repo,
            action=pr_action,
            pull_request=live_pr,
            observed_at=now,
            delivery_id=delivery_id,
        )
    except ValueError:
        if pr_action == "polled":
            raise
        # A delayed webhook can describe an earlier action after GitHub has
        # already moved the PR again. The live fetch remains authoritative, so
        # journal that current state as a poll while retaining the delivery ID.
        logger.info(
            "GitHub PR action %s no longer matches live state for changeset %s; "
            "recording the live state as polled.",
            pr_action,
            changeset_id,
        )
        pr_observation = build_pull_request_observation(
            changeset_id=changeset_id,
            repository=connection.repo,
            action="polled",
            pull_request=live_pr,
            observed_at=now,
            delivery_id=delivery_id,
        )
    await apply_pull_request_observation(pool, pr_observation)

    projected = await changeset_store.get_changeset(pool, changeset_id)
    if (
        projected is None
        or projected.status is not ChangesetStatus.pr_open
        or projected.github_pr_status
        not in {GitHubPRStatus.open, GitHubPRStatus.draft}
        or not projected.head_sha
    ):
        return None

    evidence = await get_ci_evidence(
        connection.repo,
        projected.head_sha,
        token,
    )
    observation = build_ci_verification_observation(
        changeset_id=changeset_id,
        repository=connection.repo,
        pr_number=projected.pr_number,
        head_sha=projected.head_sha,
        combined_status=evidence.combined_status,
        check_runs=evidence.check_runs,
        observed_at=datetime.now(timezone.utc),
        ledger=projected.requirement_ledger,
    )
    applied = await apply_ci_verification_observation(pool, observation)
    if (
        applied.projected
        and observation.status.value == "failed"
        and repair_failure is not None
    ):
        await repair_failure(observation)
    return observation
