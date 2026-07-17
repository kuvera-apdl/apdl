"""Crash-safe branch and pull-request publication orchestration."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any

import asyncpg
import httpx

from app.github.publisher import (
    BranchPublicationError,
    BranchPublisher,
    PublishedBranch,
)
from app.github.pulls import (
    PullRequest,
    PullRequestDiscoveryError,
    PullRequestIdentityError,
    validate_pull_request,
)
from app.models.changeset import ChangesetStatus
from app.models.observations import GitHubPRStatus, PullRequestObservation
from app.models.pr_publication import (
    PublicationBranchPublished,
    PublicationCleanupConfirmed,
    PublicationCleanupRequested,
    PublicationCreateAccepted,
    PublicationIdentityValidated,
    PublicationIntentRecorded,
    PublicationManualIntervention,
    PublicationRecoveryDeferred,
    PullRequestAcceptedReceipt,
    PullRequestPublicationEvent,
)
from app.store import changesets as changeset_store
from app.store import pr_publication as publication_store


logger = logging.getLogger(__name__)

TokenMinter = Callable[[str], AbstractAsyncContextManager[str]]
PROpener = Callable[..., Awaitable[PullRequest]]
PRFinder = Callable[..., Awaitable[PullRequest | None]]
PRCloser = Callable[..., Awaitable[None]]


def _event_id() -> str:
    return f"cpub_{uuid.uuid4().hex}"


async def _shielded(awaitable: Awaitable[Any]) -> Any:
    task = asyncio.create_task(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


async def _record_accepted(
    pool: asyncpg.Pool,
    intent: PublicationIntentRecorded,
    receipt: PullRequestAcceptedReceipt,
) -> None:
    await _shielded(
        publication_store.append_event(
            pool,
            PublicationCreateAccepted(
                event_id=_event_id(),
                changeset_id=intent.changeset_id,
                recorded_at=datetime.now(timezone.utc),
                intent_event_id=intent.event_id,
                receipt=receipt,
            ),
        )
    )


async def _record_manual(
    pool: asyncpg.Pool,
    intent: PublicationIntentRecorded,
    *,
    reason: str,
    receipt: PullRequestAcceptedReceipt | None = None,
) -> None:
    event = PublicationManualIntervention(
        event_id=_event_id(),
        changeset_id=intent.changeset_id,
        recorded_at=datetime.now(timezone.utc),
        intent_event_id=intent.event_id,
        pr_number=receipt.pr_number if receipt is not None else None,
        github_url=receipt.github_url if receipt is not None else None,
        reason=reason,
    )
    await _shielded(
        publication_store.append_terminal_event_and_error(
            pool,
            event,
            error=f"Pull-request publication requires manual intervention: {reason}",
        )
    )


async def _record_deferred(
    pool: asyncpg.Pool,
    intent: PublicationIntentRecorded,
    *,
    reason: str,
) -> None:
    await _shielded(
        publication_store.append_event(
            pool,
            PublicationRecoveryDeferred(
                event_id=_event_id(),
                changeset_id=intent.changeset_id,
                recorded_at=datetime.now(timezone.utc),
                intent_event_id=intent.event_id,
                reason=reason,
            ),
        )
    )


async def _finalize(
    pool: asyncpg.Pool,
    intent: PublicationIntentRecorded,
    pull_request: PullRequest,
    *,
    expected_head_sha: str,
) -> None:
    validated = validate_pull_request(
        pull_request,
        repository=intent.repository,
        repository_id=intent.repository_id,
        head=intent.branch,
        base=intent.base_branch,
        expected_head_sha=expected_head_sha,
    )
    current = await changeset_store.get_changeset(pool, intent.changeset_id)
    if current is None:
        raise ValueError("publication intent references an unknown changeset")
    if current.status is ChangesetStatus.pr_open:
        if (
            current.pr_number == validated.number
            and current.head_sha == validated.head_sha
        ):
            return
        raise ValueError("changeset is already projected onto a different GitHub PR")
    await _shielded(
        publication_store.append_event(
            pool,
            PublicationIdentityValidated(
                event_id=_event_id(),
                changeset_id=intent.changeset_id,
                recorded_at=datetime.now(timezone.utc),
                intent_event_id=intent.event_id,
                repository=validated.repository,
                repository_id=validated.repository_id,
                branch=validated.head_ref,
                base_branch=validated.base_ref,
                pr_number=validated.number,
                github_url=validated.url,
                head_sha=validated.head_sha,
                status=validated.status,
                github_updated_at=validated.github_updated_at,
            ),
        )
    )
    observation = PullRequestObservation(
        observation_id=f"probs_{uuid.uuid4().hex}",
        changeset_id=intent.changeset_id,
        repository=intent.repository,
        pr_number=validated.number,
        head_sha=validated.head_sha,
        status=validated.status,
        action="opened",
        github_url=validated.url,
        github_updated_at=validated.github_updated_at,
        observed_at=datetime.now(timezone.utc),
    )
    await _shielded(
        changeset_store.mark_pr_open(
            pool,
            intent.changeset_id,
            branch=intent.branch,
            observation=observation,
            external_ci_status=intent.external_ci_status,
            diff_stat=intent.diff_stat,
        )
    )


async def _cleanup_invalid(
    pool: asyncpg.Pool,
    intent: PublicationIntentRecorded,
    receipt: PullRequestAcceptedReceipt,
    *,
    reason: str,
    expected_head_sha: str,
    token: str,
    close_pr: PRCloser,
) -> None:
    if receipt.pr_number is None:
        await _record_manual(pool, intent, reason=reason, receipt=receipt)
        return
    request = PublicationCleanupRequested(
        event_id=_event_id(),
        changeset_id=intent.changeset_id,
        recorded_at=datetime.now(timezone.utc),
        intent_event_id=intent.event_id,
        pr_number=receipt.pr_number,
        github_url=receipt.github_url,
        expected_head_sha=expected_head_sha,
        next_action="terminal_error",
        reason=reason,
    )
    await _shielded(publication_store.append_event(pool, request))
    try:
        await close_pr(
            repo=intent.repository,
            repository_id=intent.repository_id,
            number=receipt.pr_number,
            head=intent.branch,
            base=intent.base_branch,
            expected_head_sha=expected_head_sha,
            token=token,
        )
    except Exception as exc:
        await _record_manual(
            pool,
            intent,
            reason=(
                f"{reason}; cleanup of PR #{receipt.pr_number} was not confirmed: {exc}"
            ),
            receipt=receipt,
        )
        return
    confirmed = PublicationCleanupConfirmed(
        event_id=_event_id(),
        changeset_id=intent.changeset_id,
        recorded_at=datetime.now(timezone.utc),
        intent_event_id=intent.event_id,
        cleanup_request_event_id=request.event_id,
        pr_number=receipt.pr_number,
        github_url=receipt.github_url,
        next_action="terminal_error",
        reason=reason,
    )
    await _shielded(
        publication_store.append_terminal_event_and_error(
            pool,
            confirmed,
            error=(
                "GitHub accepted a pull request with invalid identity; "
                f"PR #{receipt.pr_number} was closed: {reason}"
            ),
        )
    )


async def _handle_settled_pull_request(
    pool: asyncpg.Pool,
    intent: PublicationIntentRecorded,
    pull_request: PullRequest,
    accepted_receipts: list[PullRequestAcceptedReceipt],
    *,
    expected_head_sha: str,
    token: str,
    close_pr: PRCloser,
) -> bool:
    """Terminalize a deterministic branch identity that GitHub already settled."""
    if pull_request.status in {GitHubPRStatus.draft, GitHubPRStatus.open}:
        return False
    receipt = next(
        (
            candidate
            for candidate in reversed(accepted_receipts)
            if candidate.pr_number == pull_request.number
        ),
        None,
    )
    if receipt is None:
        raise RuntimeError(
            "settled pull-request recovery returned without journaling acceptance"
        )
    reason = (
        f"Deterministic APDL PR #{pull_request.number} is already "
        f"{pull_request.status.value}"
    )
    if pull_request.status is GitHubPRStatus.closed:
        await _cleanup_invalid(
            pool,
            intent,
            receipt,
            reason=reason,
            expected_head_sha=expected_head_sha,
            token=token,
            close_pr=close_pr,
        )
    else:
        await _record_manual(pool, intent, reason=reason, receipt=receipt)
    return True


async def _close_conflicting_receipt(
    pool: asyncpg.Pool,
    intent: PublicationIntentRecorded,
    receipt: PullRequestAcceptedReceipt,
    *,
    reason: str,
    expected_head_sha: str,
    token: str,
    close_pr: PRCloser,
) -> bool:
    """Close a second accepted identity before projecting the recovered PR."""
    if receipt.pr_number is None:
        await _record_manual(pool, intent, reason=reason, receipt=receipt)
        return False
    request = PublicationCleanupRequested(
        event_id=_event_id(),
        changeset_id=intent.changeset_id,
        recorded_at=datetime.now(timezone.utc),
        intent_event_id=intent.event_id,
        pr_number=receipt.pr_number,
        github_url=receipt.github_url,
        expected_head_sha=expected_head_sha,
        next_action="continue_recovered",
        reason=reason,
    )
    await _shielded(publication_store.append_event(pool, request))
    try:
        await close_pr(
            repo=intent.repository,
            repository_id=intent.repository_id,
            number=receipt.pr_number,
            head=intent.branch,
            base=intent.base_branch,
            expected_head_sha=expected_head_sha,
            token=token,
        )
    except Exception as exc:
        await _record_manual(
            pool,
            intent,
            reason=(
                f"{reason}; cleanup of conflicting PR #{receipt.pr_number} "
                f"was not confirmed: {exc}"
            ),
            receipt=receipt,
        )
        return False
    await _shielded(
        publication_store.append_event(
            pool,
            PublicationCleanupConfirmed(
                event_id=_event_id(),
                changeset_id=intent.changeset_id,
                recorded_at=datetime.now(timezone.utc),
                intent_event_id=intent.event_id,
                cleanup_request_event_id=request.event_id,
                pr_number=receipt.pr_number,
                github_url=receipt.github_url,
                next_action="continue_recovered",
                reason=reason,
            ),
        )
    )
    return True


def _receipt_matches_identity(
    receipt: PullRequestAcceptedReceipt,
    *,
    pr_number: int | None,
    github_url: str | None,
) -> bool:
    if receipt.pr_number is None or receipt.pr_number != pr_number:
        return False
    return receipt.github_url is None or receipt.github_url == github_url


def _unhandled_create_acceptances(
    events: list[PullRequestPublicationEvent],
) -> list[PublicationCreateAccepted]:
    """Reconstruct create acceptances with no later cleanup/manual/validation."""
    pending: list[PublicationCreateAccepted] = []
    for event in events:
        if (
            isinstance(event, PublicationCreateAccepted)
            and event.receipt.source == "create"
        ):
            pending.append(event)
            continue
        if isinstance(
            event,
            (
                PublicationCleanupRequested,
                PublicationIdentityValidated,
                PublicationManualIntervention,
            ),
        ):
            pending = [
                accepted
                for accepted in pending
                if not _receipt_matches_identity(
                    accepted.receipt,
                    pr_number=event.pr_number,
                    github_url=event.github_url,
                )
            ]
    return pending


def _accepted_identity_summary(
    acceptances: list[PublicationCreateAccepted],
) -> str:
    identities = [
        (
            f"#{event.receipt.pr_number}"
            if event.receipt.pr_number is not None
            else event.receipt.github_url or "unknown identity"
        )
        for event in acceptances[:20]
    ]
    if len(acceptances) > 20:
        identities.append(f"{len(acceptances) - 20} more")
    return ", ".join(identities)


async def _reconcile_unhandled_create_acceptances(
    pool: asyncpg.Pool,
    intent: PublicationIntentRecorded,
    acceptances: list[PublicationCreateAccepted],
    pull_request: PullRequest | None,
    *,
    expected_head_sha: str,
    token: str,
    close_pr: PRCloser,
) -> bool:
    """Resolve every persisted create acceptance before any new projection."""
    if not acceptances:
        return False
    if any(event.receipt.pr_number is None for event in acceptances):
        await _record_manual(
            pool,
            intent,
            reason=(
                "Persisted accepted create response has no cleanup-safe PR number: "
                f"{_accepted_identity_summary(acceptances)}"
            ),
            receipt=acceptances[0].receipt,
        )
        return True

    if pull_request is None:
        identities_by_number = {event.receipt.pr_number: event for event in acceptances}
        urls_by_number: dict[int, set[str]] = {}
        for event in acceptances:
            assert event.receipt.pr_number is not None
            if event.receipt.github_url is not None:
                urls_by_number.setdefault(event.receipt.pr_number, set()).add(
                    event.receipt.github_url
                )
        if len(identities_by_number) != 1 or any(
            len(urls) > 1 for urls in urls_by_number.values()
        ):
            await _record_manual(
                pool,
                intent,
                reason=(
                    "Multiple unhandled create acceptances remain after restart: "
                    f"{_accepted_identity_summary(acceptances)}"
                ),
                receipt=acceptances[0].receipt,
            )
            return True
        receipt = next(iter(identities_by_number.values())).receipt
        await _cleanup_invalid(
            pool,
            intent,
            receipt,
            reason=(
                "GitHub accepted create before the controller could validate "
                "its identity; deterministic branch recovery found no PR"
            ),
            expected_head_sha=expected_head_sha,
            token=token,
            close_pr=close_pr,
        )
        return True

    conflicting_by_number: dict[int, list[PullRequestAcceptedReceipt]] = {}
    for event in acceptances:
        receipt = event.receipt
        assert receipt.pr_number is not None
        if receipt.pr_number == pull_request.number:
            if (
                receipt.github_url is not None
                and receipt.github_url != pull_request.url
            ):
                await _record_manual(
                    pool,
                    intent,
                    reason=(
                        "Persisted create acceptance and deterministic branch "
                        "recovery identified the same PR number with different URLs"
                    ),
                    receipt=receipt,
                )
                return True
            continue
        conflicting_by_number.setdefault(receipt.pr_number, []).append(receipt)

    for receipts in conflicting_by_number.values():
        urls = {
            receipt.github_url for receipt in receipts if receipt.github_url is not None
        }
        if len(urls) > 1:
            await _record_manual(
                pool,
                intent,
                reason=(
                    "Persisted create acceptances identified one conflicting PR "
                    "number with different URLs"
                ),
                receipt=receipts[0],
            )
            return True
        receipt = next(
            (candidate for candidate in receipts if candidate.github_url is not None),
            receipts[0],
        )
        closed = await _close_conflicting_receipt(
            pool,
            intent,
            receipt,
            reason=(
                "Persisted accepted create response identified a different PR "
                "than deterministic branch recovery"
            ),
            expected_head_sha=expected_head_sha,
            token=token,
            close_pr=close_pr,
        )
        if not closed:
            return True
    return False


async def _resume_cleanup_state(
    pool: asyncpg.Pool,
    intent: PublicationIntentRecorded,
    *,
    mint_pr_write_token: TokenMinter,
    close_pr: PRCloser,
) -> bool:
    """Finish journaled cleanup before any branch lookup or PR creation."""
    events = await publication_store.list_events(pool, intent.changeset_id)
    manual = next(
        (
            event
            for event in reversed(events)
            if isinstance(event, PublicationManualIntervention)
        ),
        None,
    )
    if manual is not None:
        await _shielded(
            publication_store.ensure_terminal_error(
                pool,
                intent.changeset_id,
                error=(
                    "Pull-request publication requires manual intervention: "
                    f"{manual.reason}"
                ),
            )
        )
        return True

    terminal_cleanup = next(
        (
            event
            for event in reversed(events)
            if isinstance(event, PublicationCleanupConfirmed)
            and event.next_action == "terminal_error"
        ),
        None,
    )
    if terminal_cleanup is not None:
        await _shielded(
            publication_store.ensure_terminal_error(
                pool,
                intent.changeset_id,
                error=(
                    "GitHub accepted a pull request with invalid identity; "
                    f"PR #{terminal_cleanup.pr_number} was closed: "
                    f"{terminal_cleanup.reason}"
                ),
            )
        )
        return True

    confirmed_request_ids = {
        event.cleanup_request_event_id
        for event in events
        if isinstance(event, PublicationCleanupConfirmed)
    }
    pending_requests = [
        event
        for event in events
        if isinstance(event, PublicationCleanupRequested)
        and event.event_id not in confirmed_request_ids
    ]
    if not pending_requests:
        return False

    async with mint_pr_write_token(intent.changeset_id) as token:
        for request in pending_requests:
            try:
                await close_pr(
                    repo=intent.repository,
                    repository_id=intent.repository_id,
                    number=request.pr_number,
                    head=intent.branch,
                    base=intent.base_branch,
                    expected_head_sha=request.expected_head_sha,
                    token=token,
                )
            except Exception as exc:
                receipt = PullRequestAcceptedReceipt(
                    source="recovery",
                    repository=intent.repository,
                    requested_head=intent.branch,
                    requested_base=intent.base_branch,
                    accepted_at=datetime.now(timezone.utc),
                    status_code=200,
                    pr_number=request.pr_number,
                    github_url=request.github_url,
                    raw_response={
                        "cleanup_request_event_id": request.event_id,
                    },
                )
                await _record_manual(
                    pool,
                    intent,
                    reason=(
                        f"{request.reason}; resumed cleanup of PR "
                        f"#{request.pr_number} was not confirmed: {exc}"
                    ),
                    receipt=receipt,
                )
                return True

            confirmed = PublicationCleanupConfirmed(
                event_id=_event_id(),
                changeset_id=intent.changeset_id,
                recorded_at=datetime.now(timezone.utc),
                intent_event_id=intent.event_id,
                cleanup_request_event_id=request.event_id,
                pr_number=request.pr_number,
                github_url=request.github_url,
                next_action=request.next_action,
                reason=request.reason,
            )
            if confirmed.next_action == "terminal_error":
                await _shielded(
                    publication_store.append_terminal_event_and_error(
                        pool,
                        confirmed,
                        error=(
                            "GitHub accepted a pull request with invalid identity; "
                            f"PR #{confirmed.pr_number} was closed: "
                            f"{confirmed.reason}"
                        ),
                    )
                )
                return True
            await _shielded(publication_store.append_event(pool, confirmed))
    return False


def _permanent_branch_error(error: BranchPublicationError) -> bool:
    detail = str(error).lower()
    return any(
        marker in detail
        for marker in (
            "differs from the gated candidate",
            "does not match the gated candidate",
            "symbolic link",
            "not a canonical",
            "must be an exact",
            "head changed",
        )
    )


async def _ensure_branch(
    pool: asyncpg.Pool,
    intent: PublicationIntentRecorded,
    *,
    mint_read_token: TokenMinter,
    mint_write_token: TokenMinter,
    branch_publisher: BranchPublisher,
) -> PublishedBranch:
    async with mint_read_token(intent.changeset_id) as read_token:
        existing = await branch_publisher.recover_published(
            repository=intent.repository,
            branch=intent.branch,
            candidate_tree_sha=intent.candidate_tree_sha,
            read_token=read_token,
        )
        if existing is None:
            async with branch_publisher.prepare(
                repository=intent.repository,
                branch=intent.branch,
                base_branch=intent.base_branch,
                expected_base_sha=intent.candidate_base_sha,
                expected_remote_sha=None,
                candidate_head_sha=intent.candidate_head_sha,
                candidate_tree_sha=intent.candidate_tree_sha,
                patch_base64=intent.patch_base64,
                commit_title=intent.commit_title,
                read_token=read_token,
            ) as prepared:
                try:
                    async with mint_write_token(intent.changeset_id) as write_token:
                        existing = await branch_publisher.push(
                            prepared,
                            write_token=write_token,
                        )
                except BranchPublicationError:
                    # Another replica may have won the empty-branch lease.
                    existing = await branch_publisher.recover_published(
                        repository=intent.repository,
                        branch=intent.branch,
                        candidate_tree_sha=intent.candidate_tree_sha,
                        read_token=read_token,
                    )
                    if existing is None:
                        raise
    await _shielded(
        publication_store.append_event(
            pool,
            PublicationBranchPublished(
                event_id=_event_id(),
                changeset_id=intent.changeset_id,
                recorded_at=datetime.now(timezone.utc),
                intent_event_id=intent.event_id,
                branch=existing.branch,
                head_sha=existing.head_sha,
                tree_sha=intent.candidate_tree_sha,
            ),
        )
    )
    return existing


async def _resume_pull_request_publication_locked(
    pool: publication_store.PublicationConnectionPool,
    changeset_id: str,
    *,
    mint_read_token: TokenMinter,
    mint_write_token: TokenMinter,
    mint_pr_write_token: TokenMinter,
    branch_publisher: BranchPublisher,
    open_pr: PROpener,
    find_pr: PRFinder,
    close_pr: PRCloser,
) -> bool:
    """Resume one durable intent; return true when it reaches a terminal state."""
    intent = await publication_store.get_intent(pool, changeset_id)
    if intent is None:
        raise ValueError("changeset has no durable pull-request publication intent")
    current = await changeset_store.get_changeset(pool, changeset_id)
    if current is None:
        raise ValueError("publication intent references an unknown changeset")
    if current.status is ChangesetStatus.pr_open:
        return True
    if current.status is not ChangesetStatus.pushing:
        return current.status in {
            ChangesetStatus.error,
            ChangesetStatus.abandoned,
            ChangesetStatus.merged,
        }

    try:
        if await _resume_cleanup_state(
            pool,
            intent,
            mint_pr_write_token=mint_pr_write_token,
            close_pr=close_pr,
        ):
            return True
        unhandled_create_acceptances = _unhandled_create_acceptances(
            await publication_store.list_events(pool, intent.changeset_id)
        )
        published = await _ensure_branch(
            pool,
            intent,
            mint_read_token=mint_read_token,
            mint_write_token=mint_write_token,
            branch_publisher=branch_publisher,
        )
        accepted_receipts: list[PullRequestAcceptedReceipt] = []

        async def on_accepted(receipt: PullRequestAcceptedReceipt) -> None:
            await _record_accepted(pool, intent, receipt)
            accepted_receipts.append(receipt)

        async with mint_pr_write_token(changeset_id) as token:
            try:
                pull_request = await find_pr(
                    repo=intent.repository,
                    repository_id=intent.repository_id,
                    head=intent.branch,
                    base=intent.base_branch,
                    expected_head_sha=published.head_sha,
                    token=token,
                    on_accepted=on_accepted,
                )
            except PullRequestIdentityError as exc:
                if unhandled_create_acceptances:
                    await _record_manual(
                        pool,
                        intent,
                        reason=(
                            "Branch recovery returned invalid identity while "
                            "persisted create acceptances remain unhandled: "
                            f"{_accepted_identity_summary(unhandled_create_acceptances)}; "
                            f"{exc}"
                        ),
                        receipt=unhandled_create_acceptances[0].receipt,
                    )
                    return True
                await _cleanup_invalid(
                    pool,
                    intent,
                    exc.receipt,
                    reason=str(exc),
                    expected_head_sha=published.head_sha,
                    token=token,
                    close_pr=close_pr,
                )
                return True
            except PullRequestDiscoveryError as exc:
                if exc.receipts:
                    identities = ", ".join(
                        (
                            f"#{receipt.pr_number}"
                            if receipt.pr_number is not None
                            else receipt.github_url or "unknown identity"
                        )
                        for receipt in exc.receipts
                    )
                    await _record_manual(
                        pool,
                        intent,
                        reason=(
                            f"{exc}: {identities}"
                            + (
                                "; persisted unhandled create acceptances: "
                                f"{_accepted_identity_summary(unhandled_create_acceptances)}"
                                if unhandled_create_acceptances
                                else ""
                            )
                        ),
                        receipt=(
                            unhandled_create_acceptances[0].receipt
                            if unhandled_create_acceptances
                            else exc.receipts[0]
                        ),
                    )
                    return True
                raise
            if pull_request is not None and not accepted_receipts:
                raise RuntimeError(
                    "pull-request recovery returned without journaling acceptance"
                )
            if await _reconcile_unhandled_create_acceptances(
                pool,
                intent,
                unhandled_create_acceptances,
                pull_request,
                expected_head_sha=published.head_sha,
                token=token,
                close_pr=close_pr,
            ):
                return True
            if pull_request is not None and await _handle_settled_pull_request(
                pool,
                intent,
                pull_request,
                accepted_receipts,
                expected_head_sha=published.head_sha,
                token=token,
                close_pr=close_pr,
            ):
                return True
            if pull_request is None:
                accepted_before_create = len(accepted_receipts)
                try:
                    pull_request = await open_pr(
                        repo=intent.repository,
                        repository_id=intent.repository_id,
                        head=intent.branch,
                        base=intent.base_branch,
                        expected_head_sha=published.head_sha,
                        title=intent.pull_request_title,
                        body=intent.pull_request_body,
                        token=token,
                        on_accepted=on_accepted,
                        draft=intent.draft,
                    )
                except PullRequestIdentityError as exc:
                    # A malformed create response may still be recoverable by
                    # the deterministic APDL branch before cleanup is attempted.
                    try:
                        recovered = await find_pr(
                            repo=intent.repository,
                            repository_id=intent.repository_id,
                            head=intent.branch,
                            base=intent.base_branch,
                            expected_head_sha=published.head_sha,
                            token=token,
                            on_accepted=on_accepted,
                        )
                    except PullRequestIdentityError as recovery_error:
                        await _cleanup_invalid(
                            pool,
                            intent,
                            recovery_error.receipt,
                            reason=str(recovery_error),
                            expected_head_sha=published.head_sha,
                            token=token,
                            close_pr=close_pr,
                        )
                        return True
                    except PullRequestDiscoveryError as discovery_error:
                        if discovery_error.receipts:
                            identities = ", ".join(
                                (
                                    f"#{receipt.pr_number}"
                                    if receipt.pr_number is not None
                                    else receipt.github_url or "unknown identity"
                                )
                                for receipt in discovery_error.receipts
                            )
                            await _record_manual(
                                pool,
                                intent,
                                reason=f"{discovery_error}: {identities}",
                                receipt=discovery_error.receipts[0],
                            )
                            return True
                        raise
                    if recovered is not None:
                        number_conflict = (
                            exc.receipt.pr_number is not None
                            and exc.receipt.pr_number != recovered.number
                        )
                        url_conflict = (
                            exc.receipt.github_url is not None
                            and exc.receipt.github_url != recovered.url
                        )
                        if url_conflict and not number_conflict:
                            await _record_manual(
                                pool,
                                intent,
                                reason=(
                                    "Accepted create response and branch recovery "
                                    "identified the same PR number with different URLs"
                                ),
                                receipt=exc.receipt,
                            )
                            return True
                        if number_conflict:
                            closed = await _close_conflicting_receipt(
                                pool,
                                intent,
                                exc.receipt,
                                reason=(
                                    "Accepted create response identified a different "
                                    "PR than deterministic branch recovery"
                                ),
                                expected_head_sha=published.head_sha,
                                token=token,
                                close_pr=close_pr,
                            )
                            if not closed:
                                return True
                        pull_request = recovered
                    else:
                        await _cleanup_invalid(
                            pool,
                            intent,
                            exc.receipt,
                            reason=str(exc),
                            expected_head_sha=published.head_sha,
                            token=token,
                            close_pr=close_pr,
                        )
                        return True
                if len(accepted_receipts) == accepted_before_create:
                    raise RuntimeError(
                        "pull-request create returned without journaling acceptance"
                    )
            if await _handle_settled_pull_request(
                pool,
                intent,
                pull_request,
                accepted_receipts,
                expected_head_sha=published.head_sha,
                token=token,
                close_pr=close_pr,
            ):
                return True
            pull_request = validate_pull_request(
                pull_request,
                repository=intent.repository,
                repository_id=intent.repository_id,
                head=intent.branch,
                base=intent.base_branch,
                expected_head_sha=published.head_sha,
            )
        await _finalize(
            pool,
            intent,
            pull_request,
            expected_head_sha=published.head_sha,
        )
        logger.info(
            "Changeset %s opened or recovered GitHub PR %s",
            changeset_id,
            pull_request.url,
        )
        return True
    except BranchPublicationError as exc:
        if _permanent_branch_error(exc):
            await _record_manual(pool, intent, reason=str(exc))
            return True
        await _record_deferred(pool, intent, reason=str(exc))
        return False
    except PullRequestDiscoveryError as exc:
        await _record_deferred(pool, intent, reason=str(exc))
        return False
    except httpx.HTTPError as exc:
        await _record_deferred(pool, intent, reason=str(exc))
        return False
    except RuntimeError as exc:
        if "active repository grant" in str(exc).lower():
            await _record_manual(pool, intent, reason=str(exc))
            return True
        await _record_deferred(pool, intent, reason=str(exc))
        return False
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(
            "Pull-request publication recovery failed for changeset %s",
            changeset_id,
        )
        await _record_deferred(pool, intent, reason=str(exc))
        return False


async def resume_pull_request_publication(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    mint_read_token: TokenMinter,
    mint_write_token: TokenMinter,
    mint_pr_write_token: TokenMinter,
    branch_publisher: BranchPublisher,
    open_pr: PROpener,
    find_pr: PRFinder,
    close_pr: PRCloser,
) -> bool:
    """Serialize one publication across replicas and resume its durable intent."""
    async with publication_store.acquire_publication_lock(
        pool,
        changeset_id,
    ) as locked_pool:
        if locked_pool is None:
            logger.info(
                "Changeset %s publication is active on another worker",
                changeset_id,
            )
            return False
        return await _resume_pull_request_publication_locked(
            locked_pool,
            changeset_id,
            mint_read_token=mint_read_token,
            mint_write_token=mint_write_token,
            mint_pr_write_token=mint_pr_write_token,
            branch_publisher=branch_publisher,
            open_pr=open_pr,
            find_pr=find_pr,
            close_pr=close_pr,
        )
