"""Recover GitHub PR and exact-head CI observations without owning either."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
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
from app.runtime.collector import RuntimeEvidenceCollection
from app.runtime.evidence import build_runtime_evidence_observation
from app.runtime.models import RuntimeAcceptancePlan
from app.store import changesets as changeset_store
from app.store import connections as connections_store
from app.store.observations import (
    apply_ci_verification_observation,
    apply_pull_request_observation,
)
from app.store.runtime_evidence import (
    apply_runtime_evidence_observation,
    claim_runtime_evidence_collection,
    release_runtime_evidence_collection,
)

logger = logging.getLogger(__name__)

PullRequestReader = Callable[[str, int, str], Awaitable[dict[str, Any]]]
CIEvidenceReader = Callable[[str, str, str], Awaitable[GitHubCIEvidence]]
TokenMinter = Callable[[str], AbstractAsyncContextManager[str]]
CIFailureHandler = Callable[[CIVerificationObservation], Awaitable[None]]
RuntimeEvidenceCollector = Callable[
    [str, str, str, RuntimeAcceptancePlan], Awaitable[RuntimeEvidenceCollection]
]
_runtime_collection_semaphore: asyncio.Semaphore | None = None


def _runtime_collection_slot() -> asyncio.Semaphore:
    global _runtime_collection_semaphore
    if _runtime_collection_semaphore is None:
        _runtime_collection_semaphore = asyncio.Semaphore(2)
    return _runtime_collection_semaphore


async def sync_github_state(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    get_pull_request: PullRequestReader,
    get_ci_evidence: CIEvidenceReader,
    mint_token: TokenMinter,
    repair_failure: CIFailureHandler | None = None,
    collect_runtime: RuntimeEvidenceCollector | None = None,
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
    connection = await connections_store.get_connection_for_changeset(
        pool, changeset_id
    )
    if connection is None:
        return None
    async with mint_token(changeset_id) as token:
        live_pr = await get_pull_request(
            connection.repository_full_name, changeset.pr_number, token
        )
        try:
            pr_observation = build_pull_request_observation(
                changeset_id=changeset_id,
                repository=connection.repository_full_name,
                action=pr_action,
                pull_request=live_pr,
                observed_at=datetime.now(timezone.utc),
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
                repository=connection.repository_full_name,
                action="polled",
                pull_request=live_pr,
                observed_at=datetime.now(timezone.utc),
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
            connection.repository_full_name,
            projected.head_sha,
            token,
        )
        observation = build_ci_verification_observation(
            changeset_id=changeset_id,
            repository=connection.repository_full_name,
            pr_number=projected.pr_number,
            head_sha=projected.head_sha,
            combined_status=evidence.combined_status,
            check_runs=evidence.check_runs,
            observed_at=datetime.now(timezone.utc),
            ledger=projected.requirement_ledger,
        )
        await apply_ci_verification_observation(pool, observation)
        runtime_claimed = False
        if (
            collect_runtime is not None
            and projected.runtime_acceptance_plan is not None
            and projected.runtime_acceptance_plan.checks
        ):
            try:
                runtime_claimed = await claim_runtime_evidence_collection(
                    pool,
                    changeset_id=changeset_id,
                    head_sha=projected.head_sha,
                    ci_observation_id=observation.observation_id,
                )
                if runtime_claimed:
                    async with _runtime_collection_slot():
                        collection = await collect_runtime(
                            connection.repository_full_name,
                            projected.head_sha,
                            token,
                            projected.runtime_acceptance_plan,
                        )
                        runtime_observation = build_runtime_evidence_observation(
                            changeset_id=changeset_id,
                            repository=connection.repository_full_name,
                            pr_number=projected.pr_number,
                            head_sha=projected.head_sha,
                            ci_observation=observation,
                            plan=projected.runtime_acceptance_plan,
                            collection=collection,
                            observed_at=datetime.now(timezone.utc),
                        )
                        await apply_runtime_evidence_observation(
                            pool, runtime_observation
                        )
            except Exception:
                # External Actions/artifact availability cannot prevent CI projection
                # or wedge the repair loop. Known 403/missing cases are returned as
                # explicit collector diagnostics; this is a last-resort isolation.
                logger.warning(
                    "Runtime evidence collection failed for changeset %s head %s",
                    changeset_id,
                    projected.head_sha,
                    exc_info=True,
                )
                if runtime_claimed:
                    try:
                        await release_runtime_evidence_collection(
                            pool,
                            changeset_id=changeset_id,
                            head_sha=projected.head_sha,
                            ci_observation_id=observation.observation_id,
                        )
                    except Exception:
                        logger.warning(
                            "Could not release runtime collection lease for %s",
                            observation.observation_id,
                            exc_info=True,
                        )
    if (
        observation.status.value == "failed"
        and repair_failure is not None
    ):
        await repair_failure(observation)
    return observation
