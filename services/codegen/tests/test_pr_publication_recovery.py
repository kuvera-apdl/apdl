"""Durable, idempotent pull-request publication recovery."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from app.github.pulls import (
    PullRequest,
    PullRequestDiscoveryError,
    PullRequestIdentityError,
)
from app.github.publisher import PublishedBranch
from app.jobs.pr_publication import resume_pull_request_publication
from app.models.changeset import ChangesetStatus
from app.models.observations import ExternalCIStatus, GitHubPRStatus
from app.models.pr_publication import (
    PublicationCleanupConfirmed,
    PublicationCleanupRequested,
    PublicationCreateAccepted,
    PublicationManualIntervention,
    PullRequestAcceptedReceipt,
    PublicationIntentRecorded,
)
from app.store import changesets as changeset_store
from app.store import pr_publication as publication_store
from tests.fakes import FakePool
from tests.publisher_fakes import FakeBranchPublisher


_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
_BRANCH = "apdl/add-dark-mode-cs_123"
_HEAD = "c" * 40


@asynccontextmanager
async def _mint(_changeset_id: str):
    yield "token"


def _intent(changeset_id: str) -> PublicationIntentRecorded:
    return PublicationIntentRecorded(
        event_id="cpub_" + "1" * 32,
        changeset_id=changeset_id,
        recorded_at=_NOW,
        repository="acme/widgets",
        repository_id=10,
        installation_id=1,
        branch=_BRANCH,
        base_branch="main",
        candidate_base_sha="a" * 40,
        candidate_head_sha=_HEAD,
        candidate_tree_sha="b" * 40,
        patch_base64=base64.b64encode(b"patch").decode(),
        commit_title="Add dark mode",
        pull_request_title="Add dark mode",
        pull_request_body="body",
        draft=True,
        external_ci_status=ExternalCIStatus.pending,
        diff_stat={"files": 1, "additions": 2, "deletions": 0},
    )


def _receipt(
    number: int,
    *,
    source: str = "recovery",
    url: str | None = None,
) -> PullRequestAcceptedReceipt:
    github_url = url or f"https://github.com/acme/widgets/pull/{number}"
    return PullRequestAcceptedReceipt(
        source=source,
        repository="acme/widgets",
        requested_head=_BRANCH,
        requested_base="main",
        accepted_at=_NOW,
        status_code=200 if source == "recovery" else 201,
        pr_number=number,
        github_url=github_url,
        raw_response={"number": number, "html_url": github_url},
    )


def _pr(
    number: int,
    *,
    status: GitHubPRStatus = GitHubPRStatus.draft,
) -> PullRequest:
    return PullRequest(
        repository="acme/widgets",
        repository_id=10,
        url=f"https://github.com/acme/widgets/pull/{number}",
        number=number,
        head_ref=_BRANCH,
        base_ref="main",
        head_sha=_HEAD,
        status=status,
        github_updated_at=_NOW,
    )


async def _seed(changeset_id: str) -> tuple[FakePool, FakeBranchPublisher]:
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset(changeset_id, "demo", status="pushing")
    await publication_store.record_intent(pool, _intent(changeset_id))
    return pool, FakeBranchPublisher()


def _cleanup_request(
    changeset_id: str,
    *,
    marker: str,
    number: int,
    next_action: Literal["terminal_error", "continue_recovered"],
    recorded_at: datetime = _NOW,
) -> PublicationCleanupRequested:
    return PublicationCleanupRequested(
        event_id=f"cpub_{marker * 32}",
        changeset_id=changeset_id,
        recorded_at=recorded_at,
        intent_event_id="cpub_" + "1" * 32,
        pr_number=number,
        github_url=f"https://github.com/acme/widgets/pull/{number}",
        expected_head_sha=_HEAD,
        next_action=next_action,
        reason=f"cleanup {number}",
    )


@pytest.mark.asyncio
async def test_recovery_finds_branch_pr_before_create_and_projects_it():
    pool, publisher = await _seed("cs_recover_existing")
    publisher.published[_BRANCH] = PublishedBranch(branch=_BRANCH, head_sha=_HEAD)
    opened = []

    async def find_pr(**kwargs):
        await kwargs["on_accepted"](_receipt(12))
        return _pr(12)

    async def open_pr(**kwargs):
        opened.append(kwargs)
        raise AssertionError("recovery must search before POST")

    async def close_pr(**_kwargs):
        raise AssertionError("valid recovery must not close the PR")

    completed = await resume_pull_request_publication(
        pool,
        "cs_recover_existing",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=find_pr,
        close_pr=close_pr,
    )

    final = await changeset_store.get_changeset(pool, "cs_recover_existing")
    assert completed is True
    assert final.status is ChangesetStatus.pr_open
    assert final.pr_number == 12
    assert opened == []


@pytest.mark.asyncio
async def test_invalid_accepted_identity_is_retained_and_closed():
    pool, publisher = await _seed("cs_invalid_created")
    find_calls = 0
    closed = []

    async def find_pr(**_kwargs):
        nonlocal find_calls
        find_calls += 1
        return None

    async def open_pr(**kwargs):
        receipt = _receipt(41, source="create")
        await kwargs["on_accepted"](receipt)
        raise PullRequestIdentityError("exact-head mismatch", receipt)

    async def close_pr(**kwargs):
        closed.append(kwargs["number"])

    completed = await resume_pull_request_publication(
        pool,
        "cs_invalid_created",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=find_pr,
        close_pr=close_pr,
    )

    final = await changeset_store.get_changeset(pool, "cs_invalid_created")
    events = await publication_store.list_events(pool, "cs_invalid_created")
    assert completed is True
    assert final.status is ChangesetStatus.error
    assert final.pr_number == 41
    assert final.pr_url.endswith("/pull/41")
    assert closed == [41]
    assert find_calls == 2
    assert any(isinstance(event, PublicationCreateAccepted) for event in events)
    assert any(isinstance(event, PublicationCleanupConfirmed) for event in events)


@pytest.mark.asyncio
async def test_different_create_identity_is_closed_before_recovered_pr_is_projected():
    pool, publisher = await _seed("cs_conflicting_created")
    find_calls = 0
    closed = []

    async def find_pr(**kwargs):
        nonlocal find_calls
        find_calls += 1
        if find_calls == 1:
            return None
        await kwargs["on_accepted"](_receipt(42))
        return _pr(42)

    async def open_pr(**kwargs):
        receipt = _receipt(41, source="create")
        await kwargs["on_accepted"](receipt)
        raise PullRequestIdentityError("malformed accepted response", receipt)

    async def close_pr(**kwargs):
        closed.append(kwargs["number"])

    completed = await resume_pull_request_publication(
        pool,
        "cs_conflicting_created",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=find_pr,
        close_pr=close_pr,
    )

    final = await changeset_store.get_changeset(pool, "cs_conflicting_created")
    assert completed is True
    assert final.status is ChangesetStatus.pr_open
    assert final.pr_number == 42
    assert closed == [41]


@pytest.mark.asyncio
async def test_conflicting_url_for_recovered_number_requires_manual_intervention():
    pool, publisher = await _seed("cs_conflicting_url")
    find_calls = 0

    async def find_pr(**kwargs):
        nonlocal find_calls
        find_calls += 1
        if find_calls == 1:
            return None
        await kwargs["on_accepted"](_receipt(42))
        return _pr(42)

    async def open_pr(**kwargs):
        receipt = _receipt(
            42,
            source="create",
            url="https://github.com/acme/widgets/pull/99",
        )
        await kwargs["on_accepted"](receipt)
        raise PullRequestIdentityError("conflicting URL", receipt)

    async def close_pr(**_kwargs):
        raise AssertionError("the recovered valid PR number must not be closed")

    completed = await resume_pull_request_publication(
        pool,
        "cs_conflicting_url",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=find_pr,
        close_pr=close_pr,
    )

    final = await changeset_store.get_changeset(pool, "cs_conflicting_url")
    events = await publication_store.list_events(pool, "cs_conflicting_url")
    assert completed is True
    assert final.status is ChangesetStatus.error
    assert final.pr_number == 42
    assert any(isinstance(event, PublicationManualIntervention) for event in events)


@pytest.mark.asyncio
async def test_ambiguous_live_branch_identities_require_manual_intervention():
    pool, publisher = await _seed("cs_ambiguous")
    receipts = (_receipt(51), _receipt(52))

    async def find_pr(**kwargs):
        for receipt in receipts:
            await kwargs["on_accepted"](receipt)
        raise PullRequestDiscoveryError("ambiguous", receipts)

    async def open_pr(**_kwargs):
        raise AssertionError("ambiguous recovery must not create another PR")

    async def close_pr(**_kwargs):
        raise AssertionError("ambiguous identities require explicit intervention")

    completed = await resume_pull_request_publication(
        pool,
        "cs_ambiguous",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=find_pr,
        close_pr=close_pr,
    )

    final = await changeset_store.get_changeset(pool, "cs_ambiguous")
    events = await publication_store.list_events(pool, "cs_ambiguous")
    assert completed is True
    assert final.status is ChangesetStatus.error
    assert {
        event.receipt.pr_number
        for event in events
        if isinstance(event, PublicationCreateAccepted)
    } == {51, 52}
    assert any(isinstance(event, PublicationManualIntervention) for event in events)


@pytest.mark.asyncio
async def test_truncated_branch_recovery_never_creates_or_cleans_up():
    pool, publisher = await _seed("cs_truncated_recovery")
    receipt = _receipt(53)

    async def find_pr(**kwargs):
        await kwargs["on_accepted"](receipt)
        raise PullRequestDiscoveryError(
            "GitHub branch recovery pagination is incomplete",
            (receipt,),
        )

    async def open_pr(**_kwargs):
        raise AssertionError("truncated recovery must not create another PR")

    async def close_pr(**_kwargs):
        raise AssertionError("truncated recovery must not clean up partial evidence")

    completed = await resume_pull_request_publication(
        pool,
        "cs_truncated_recovery",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=find_pr,
        close_pr=close_pr,
    )

    final = await changeset_store.get_changeset(pool, "cs_truncated_recovery")
    events = await publication_store.list_events(pool, "cs_truncated_recovery")
    assert completed is True
    assert final.status is ChangesetStatus.error
    assert "pagination is incomplete" in (final.error or "")
    assert any(isinstance(event, PublicationManualIntervention) for event in events)
    assert not any(isinstance(event, PublicationCleanupRequested) for event in events)


@pytest.mark.asyncio
async def test_unresolved_cleanup_finishes_before_branch_lookup_or_create():
    pool, publisher = await _seed("cs_pending_cleanup")
    request = _cleanup_request(
        "cs_pending_cleanup",
        marker="2",
        number=61,
        next_action="terminal_error",
    )
    await publication_store.append_event(pool, request)
    closed = []

    async def close_pr(**kwargs):
        closed.append(kwargs)

    async def find_pr(**_kwargs):
        raise AssertionError("cleanup must finish before pull-request discovery")

    async def open_pr(**_kwargs):
        raise AssertionError("cleanup must finish before pull-request creation")

    completed = await resume_pull_request_publication(
        pool,
        "cs_pending_cleanup",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=find_pr,
        close_pr=close_pr,
    )

    final = await changeset_store.get_changeset(pool, "cs_pending_cleanup")
    events = await publication_store.list_events(pool, "cs_pending_cleanup")
    confirmations = [
        event for event in events if isinstance(event, PublicationCleanupConfirmed)
    ]
    assert completed is True
    assert final.status is ChangesetStatus.error
    assert [call["number"] for call in closed] == [61]
    assert closed[0]["repository_id"] == 10
    assert closed[0]["head"] == _BRANCH
    assert closed[0]["base"] == "main"
    assert closed[0]["expected_head_sha"] == _HEAD
    assert confirmations[0].cleanup_request_event_id == request.event_id


@pytest.mark.asyncio
async def test_cancel_after_external_close_replays_exact_cleanup_request(
    monkeypatch,
):
    pool, publisher = await _seed("cs_cancel_cleanup")
    request = _cleanup_request(
        "cs_cancel_cleanup",
        marker="3",
        number=62,
        next_action="terminal_error",
    )
    await publication_store.append_event(pool, request)
    closed = []

    async def close_pr(**kwargs):
        closed.append(kwargs["number"])

    async def find_pr(**_kwargs):
        raise AssertionError("pending cleanup must block discovery")

    async def open_pr(**_kwargs):
        raise AssertionError("pending cleanup must block creation")

    real_append_terminal = publication_store.append_terminal_event_and_error
    interrupted = False

    async def cancel_terminal_write(*args, **kwargs):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise asyncio.CancelledError
        return await real_append_terminal(*args, **kwargs)

    monkeypatch.setattr(
        publication_store,
        "append_terminal_event_and_error",
        cancel_terminal_write,
    )
    with pytest.raises(asyncio.CancelledError):
        await resume_pull_request_publication(
            pool,
            "cs_cancel_cleanup",
            mint_read_token=_mint,
            mint_write_token=_mint,
            mint_pr_write_token=_mint,
            branch_publisher=publisher,
            open_pr=open_pr,
            find_pr=find_pr,
            close_pr=close_pr,
        )

    interrupted_state = await changeset_store.get_changeset(pool, "cs_cancel_cleanup")
    interrupted_events = await publication_store.list_events(pool, "cs_cancel_cleanup")
    assert interrupted_state.status is ChangesetStatus.pushing
    assert not any(
        isinstance(event, PublicationCleanupConfirmed) for event in interrupted_events
    )

    monkeypatch.setattr(
        publication_store,
        "append_terminal_event_and_error",
        real_append_terminal,
    )
    completed = await resume_pull_request_publication(
        pool,
        "cs_cancel_cleanup",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=find_pr,
        close_pr=close_pr,
    )

    final = await changeset_store.get_changeset(pool, "cs_cancel_cleanup")
    events = await publication_store.list_events(pool, "cs_cancel_cleanup")
    confirmation = next(
        event for event in events if isinstance(event, PublicationCleanupConfirmed)
    )
    assert completed is True
    assert final.status is ChangesetStatus.error
    assert closed == [62, 62]
    assert confirmation.cleanup_request_event_id == request.event_id


@pytest.mark.asyncio
async def test_repeated_external_cancellation_waits_for_terminal_commit(
    monkeypatch,
):
    pool, publisher = await _seed("cs_external_cancel")
    request = _cleanup_request(
        "cs_external_cancel",
        marker="a",
        number=63,
        next_action="terminal_error",
    )
    await publication_store.append_event(pool, request)
    terminal_write_started = asyncio.Event()
    release_terminal_write = asyncio.Event()
    real_append_terminal = publication_store.append_terminal_event_and_error

    async def delayed_terminal_write(*args, **kwargs):
        terminal_write_started.set()
        await release_terminal_write.wait()
        await real_append_terminal(*args, **kwargs)

    monkeypatch.setattr(
        publication_store,
        "append_terminal_event_and_error",
        delayed_terminal_write,
    )

    async def close_pr(**_kwargs):
        return None

    async def find_pr(**_kwargs):
        raise AssertionError("terminal cleanup must block discovery")

    async def open_pr(**_kwargs):
        raise AssertionError("terminal cleanup must block creation")

    task = asyncio.create_task(
        resume_pull_request_publication(
            pool,
            "cs_external_cancel",
            mint_read_token=_mint,
            mint_write_token=_mint,
            mint_pr_write_token=_mint,
            branch_publisher=publisher,
            open_pr=open_pr,
            find_pr=find_pr,
            close_pr=close_pr,
        )
    )
    await terminal_write_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release_terminal_write.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    final = await changeset_store.get_changeset(pool, "cs_external_cancel")
    events = await publication_store.list_events(pool, "cs_external_cancel")
    confirmation = next(
        event for event in events if isinstance(event, PublicationCleanupConfirmed)
    )
    assert final.status is ChangesetStatus.error
    assert confirmation.cleanup_request_event_id == request.event_id


@pytest.mark.asyncio
async def test_interleaved_cleanup_confirmations_do_not_hide_pending_request():
    pool, publisher = await _seed("cs_interleaved_cleanup")
    request_a = _cleanup_request(
        "cs_interleaved_cleanup",
        marker="4",
        number=71,
        next_action="continue_recovered",
        recorded_at=_NOW + timedelta(hours=2),
    )
    request_b = _cleanup_request(
        "cs_interleaved_cleanup",
        marker="5",
        number=72,
        next_action="terminal_error",
        recorded_at=_NOW - timedelta(hours=2),
    )
    await publication_store.append_event(pool, request_a)
    await publication_store.append_event(pool, request_b)
    await publication_store.append_event(
        pool,
        PublicationCleanupConfirmed(
            event_id="cpub_" + "6" * 32,
            changeset_id="cs_interleaved_cleanup",
            recorded_at=_NOW,
            intent_event_id="cpub_" + "1" * 32,
            cleanup_request_event_id=request_a.event_id,
            pr_number=request_a.pr_number,
            github_url=request_a.github_url,
            next_action=request_a.next_action,
            reason=request_a.reason,
        ),
    )
    closed = []

    async def close_pr(**kwargs):
        closed.append(kwargs["number"])

    async def find_pr(**_kwargs):
        raise AssertionError("terminal pending cleanup must block discovery")

    async def open_pr(**_kwargs):
        raise AssertionError("terminal pending cleanup must block creation")

    completed = await resume_pull_request_publication(
        pool,
        "cs_interleaved_cleanup",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=find_pr,
        close_pr=close_pr,
    )

    events = await publication_store.list_events(pool, "cs_interleaved_cleanup")
    confirmations = [
        event for event in events if isinstance(event, PublicationCleanupConfirmed)
    ]
    assert completed is True
    assert closed == [72]
    assert {event.cleanup_request_event_id for event in confirmations} == {
        request_a.event_id,
        request_b.event_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expects_cleanup"),
    [
        (GitHubPRStatus.closed, True),
        (GitHubPRStatus.merged, False),
    ],
)
async def test_settled_branch_identity_never_creates_duplicate(
    status,
    expects_cleanup,
):
    pool, publisher = await _seed(f"cs_settled_{status.value}")
    publisher.published[_BRANCH] = PublishedBranch(branch=_BRANCH, head_sha=_HEAD)
    closed = []

    async def find_pr(**kwargs):
        await kwargs["on_accepted"](_receipt(81))
        return _pr(81, status=status)

    async def open_pr(**_kwargs):
        raise AssertionError("settled deterministic branch must not create a PR")

    async def close_pr(**kwargs):
        closed.append(kwargs["number"])

    completed = await resume_pull_request_publication(
        pool,
        f"cs_settled_{status.value}",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=find_pr,
        close_pr=close_pr,
    )

    final = await changeset_store.get_changeset(pool, f"cs_settled_{status.value}")
    events = await publication_store.list_events(pool, f"cs_settled_{status.value}")
    assert completed is True
    assert final.status is ChangesetStatus.error
    assert closed == ([81] if expects_cleanup else [])
    assert any(
        isinstance(
            event,
            PublicationCleanupConfirmed
            if expects_cleanup
            else PublicationManualIntervention,
        )
        for event in events
    )


@pytest.mark.asyncio
async def test_replay_selects_open_pr_after_conflicting_cleanup_was_confirmed():
    pool, publisher = await _seed("cs_cleanup_then_open")
    publisher.published[_BRANCH] = PublishedBranch(branch=_BRANCH, head_sha=_HEAD)
    request = _cleanup_request(
        "cs_cleanup_then_open",
        marker="7",
        number=41,
        next_action="continue_recovered",
    )
    await publication_store.append_event(pool, request)
    await publication_store.append_event(
        pool,
        PublicationCleanupConfirmed(
            event_id="cpub_" + "8" * 32,
            changeset_id="cs_cleanup_then_open",
            recorded_at=_NOW,
            intent_event_id="cpub_" + "1" * 32,
            cleanup_request_event_id=request.event_id,
            pr_number=request.pr_number,
            github_url=request.github_url,
            next_action=request.next_action,
            reason=request.reason,
        ),
    )

    async def find_pr(**kwargs):
        await kwargs["on_accepted"](_receipt(41))
        await kwargs["on_accepted"](_receipt(42))
        return _pr(42)

    async def open_pr(**_kwargs):
        raise AssertionError("replay must recover open PR #42 without POST")

    async def close_pr(**_kwargs):
        raise AssertionError("confirmed cleanup must not be repeated")

    completed = await resume_pull_request_publication(
        pool,
        "cs_cleanup_then_open",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=find_pr,
        close_pr=close_pr,
    )

    final = await changeset_store.get_changeset(pool, "cs_cleanup_then_open")
    assert completed is True
    assert final.status is ChangesetStatus.pr_open
    assert final.pr_number == 42


@pytest.mark.asyncio
async def test_restart_reconciles_persisted_create_before_projecting_other_pr():
    pool, publisher = await _seed("cs_persisted_create_conflict")
    publisher.published[_BRANCH] = PublishedBranch(branch=_BRANCH, head_sha=_HEAD)
    await publication_store.append_event(
        pool,
        PublicationCreateAccepted(
            event_id="cpub_" + "b" * 32,
            changeset_id="cs_persisted_create_conflict",
            recorded_at=_NOW,
            intent_event_id="cpub_" + "1" * 32,
            receipt=_receipt(41, source="create"),
        ),
    )
    closed = []

    async def find_pr(**kwargs):
        await kwargs["on_accepted"](_receipt(42))
        return _pr(42)

    async def open_pr(**_kwargs):
        raise AssertionError("persisted accepted create must prevent another POST")

    async def close_pr(**kwargs):
        closed.append(kwargs["number"])

    completed = await resume_pull_request_publication(
        pool,
        "cs_persisted_create_conflict",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=find_pr,
        close_pr=close_pr,
    )

    final = await changeset_store.get_changeset(pool, "cs_persisted_create_conflict")
    events = await publication_store.list_events(pool, "cs_persisted_create_conflict")
    cleanup = next(
        event
        for event in events
        if isinstance(event, PublicationCleanupConfirmed) and event.pr_number == 41
    )
    assert completed is True
    assert closed == [41]
    assert cleanup.next_action == "continue_recovered"
    assert final.status is ChangesetStatus.pr_open
    assert final.pr_number == 42


@pytest.mark.asyncio
async def test_restart_never_posts_while_persisted_create_is_unhandled():
    pool, publisher = await _seed("cs_persisted_create_without_recovery")
    await publication_store.append_event(
        pool,
        PublicationCreateAccepted(
            event_id="cpub_" + "c" * 32,
            changeset_id="cs_persisted_create_without_recovery",
            recorded_at=_NOW,
            intent_event_id="cpub_" + "1" * 32,
            receipt=_receipt(43, source="create"),
        ),
    )
    closed = []

    async def find_pr(**_kwargs):
        return None

    async def open_pr(**_kwargs):
        raise AssertionError("unhandled accepted create must prevent another POST")

    async def close_pr(**kwargs):
        closed.append(kwargs["number"])

    completed = await resume_pull_request_publication(
        pool,
        "cs_persisted_create_without_recovery",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=find_pr,
        close_pr=close_pr,
    )

    final = await changeset_store.get_changeset(
        pool, "cs_persisted_create_without_recovery"
    )
    events = await publication_store.list_events(
        pool, "cs_persisted_create_without_recovery"
    )
    assert completed is True
    assert closed == [43]
    assert final.status is ChangesetStatus.error
    assert any(
        isinstance(event, PublicationCleanupConfirmed)
        and event.pr_number == 43
        and event.next_action == "terminal_error"
        for event in events
    )


@pytest.mark.asyncio
async def test_manual_event_and_error_projection_are_atomic():
    pool, _publisher = await _seed("cs_atomic_manual")
    event = PublicationManualIntervention(
        event_id="cpub_" + "9" * 32,
        changeset_id="cs_atomic_manual",
        recorded_at=_NOW,
        intent_event_id="cpub_" + "1" * 32,
        pr_number=91,
        github_url="https://github.com/acme/widgets/pull/91",
        reason="operator review required",
    )

    await publication_store.append_terminal_event_and_error(
        pool,
        event,
        error="manual intervention",
    )

    final = await changeset_store.get_changeset(pool, "cs_atomic_manual")
    events = await publication_store.list_events(pool, "cs_atomic_manual")
    assert final.status is ChangesetStatus.error
    assert event in events


@pytest.mark.asyncio
async def test_concurrent_replicas_do_not_interleave_publication_recovery():
    pool, publisher = await _seed("cs_serialized_recovery")
    publisher.published[_BRANCH] = PublishedBranch(branch=_BRANCH, head_sha=_HEAD)
    acquires_before_resume = pool.acquire_count
    discovery_started = asyncio.Event()
    release_discovery = asyncio.Event()
    find_calls = 0

    async def find_pr(**kwargs):
        nonlocal find_calls
        find_calls += 1
        discovery_started.set()
        await release_discovery.wait()
        await kwargs["on_accepted"](_receipt(42))
        return _pr(42)

    async def open_pr(**_kwargs):
        raise AssertionError("serialized recovery must find the existing PR")

    async def close_pr(**_kwargs):
        raise AssertionError("valid recovery must not close the PR")

    first = asyncio.create_task(
        resume_pull_request_publication(
            pool,
            "cs_serialized_recovery",
            mint_read_token=_mint,
            mint_write_token=_mint,
            mint_pr_write_token=_mint,
            branch_publisher=publisher,
            open_pr=open_pr,
            find_pr=find_pr,
            close_pr=close_pr,
        )
    )
    await discovery_started.wait()

    second = await asyncio.wait_for(
        resume_pull_request_publication(
            pool,
            "cs_serialized_recovery",
            mint_read_token=_mint,
            mint_write_token=_mint,
            mint_pr_write_token=_mint,
            branch_publisher=publisher,
            open_pr=open_pr,
            find_pr=find_pr,
            close_pr=close_pr,
        ),
        timeout=1,
    )
    assert second is False
    assert find_calls == 1

    release_discovery.set()
    assert await first is True
    # One pool checkout per replica: all store work on the winner reuses the
    # advisory-lock-owning connection instead of nesting more pool acquires.
    assert pool.acquire_count == acquires_before_resume + 2

    async def retry_find_pr(**_kwargs):
        raise AssertionError("pr_open retry must not call GitHub")

    assert (
        await resume_pull_request_publication(
            pool,
            "cs_serialized_recovery",
            mint_read_token=_mint,
            mint_write_token=_mint,
            mint_pr_write_token=_mint,
            branch_publisher=publisher,
            open_pr=open_pr,
            find_pr=retry_find_pr,
            close_pr=close_pr,
        )
        is True
    )
    final = await changeset_store.get_changeset(pool, "cs_serialized_recovery")
    assert final.status is ChangesetStatus.pr_open
    assert final.pr_number == 42
    assert pool.store["advisory_locks"] == {}


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_exact_lock_release():
    pool, publisher = await _seed("cs_cancelled_lock")
    publisher.published[_BRANCH] = PublishedBranch(branch=_BRANCH, head_sha=_HEAD)
    discovery_started = asyncio.Event()
    never_release_discovery = asyncio.Event()
    unlock_started = asyncio.Event()
    release_unlock = asyncio.Event()
    pool.store["advisory_unlock_started"] = unlock_started
    pool.store["advisory_unlock_release"] = release_unlock

    async def blocking_find_pr(**_kwargs):
        discovery_started.set()
        await never_release_discovery.wait()
        raise AssertionError("unreachable")

    async def open_pr(**_kwargs):
        raise AssertionError("cancelled recovery must not create a PR")

    async def close_pr(**_kwargs):
        raise AssertionError("cancelled recovery must not close a PR")

    task = asyncio.create_task(
        resume_pull_request_publication(
            pool,
            "cs_cancelled_lock",
            mint_read_token=_mint,
            mint_write_token=_mint,
            mint_pr_write_token=_mint,
            branch_publisher=publisher,
            open_pr=open_pr,
            find_pr=blocking_find_pr,
            close_pr=close_pr,
        )
    )
    await discovery_started.wait()
    task.cancel()
    await unlock_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release_unlock.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.store["advisory_locks"] == {}
    del pool.store["advisory_unlock_started"]
    del pool.store["advisory_unlock_release"]

    async def recovered_find_pr(**kwargs):
        await kwargs["on_accepted"](_receipt(43))
        return _pr(43)

    completed = await resume_pull_request_publication(
        pool,
        "cs_cancelled_lock",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=recovered_find_pr,
        close_pr=close_pr,
    )
    final = await changeset_store.get_changeset(pool, "cs_cancelled_lock")
    assert completed is True
    assert final.status is ChangesetStatus.pr_open
    assert final.pr_number == 43


@pytest.mark.asyncio
async def test_lock_is_released_when_initial_state_read_errors(monkeypatch):
    pool, publisher = await _seed("cs_lock_read_error")
    publisher.published[_BRANCH] = PublishedBranch(branch=_BRANCH, head_sha=_HEAD)
    real_get_intent = publication_store.get_intent
    calls = 0

    async def fail_once(locked_pool, changeset_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("state read failed")
        return await real_get_intent(locked_pool, changeset_id)

    monkeypatch.setattr(publication_store, "get_intent", fail_once)

    async def find_pr(**kwargs):
        await kwargs["on_accepted"](_receipt(44))
        return _pr(44)

    async def open_pr(**_kwargs):
        raise AssertionError("recovery must find the existing PR")

    async def close_pr(**_kwargs):
        raise AssertionError("valid recovery must not close the PR")

    with pytest.raises(RuntimeError, match="state read failed"):
        await resume_pull_request_publication(
            pool,
            "cs_lock_read_error",
            mint_read_token=_mint,
            mint_write_token=_mint,
            mint_pr_write_token=_mint,
            branch_publisher=publisher,
            open_pr=open_pr,
            find_pr=find_pr,
            close_pr=close_pr,
        )
    assert pool.store["advisory_locks"] == {}

    completed = await resume_pull_request_publication(
        pool,
        "cs_lock_read_error",
        mint_read_token=_mint,
        mint_write_token=_mint,
        mint_pr_write_token=_mint,
        branch_publisher=publisher,
        open_pr=open_pr,
        find_pr=find_pr,
        close_pr=close_pr,
    )
    final = await changeset_store.get_changeset(pool, "cs_lock_read_error")
    assert completed is True
    assert final.status is ChangesetStatus.pr_open
    assert final.pr_number == 44
