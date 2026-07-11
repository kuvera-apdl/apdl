"""Tests for CI status sync (fake pool + injected status reader)."""

from datetime import datetime, timezone

import pytest

from app.github.checks import CIStatus
from app.jobs import ci as ci_module
from app.jobs.ci import sync_ci_status
from app.models.changeset import ChangesetStatus
from app.store import changesets as store
from tests.fakes import FakePool


async def _mint(installation_id: int, repo: str) -> str:
    return "ghs_tok"


@pytest.mark.asyncio
async def test_sync_marks_passed_and_promotes_ready():
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset(
        "cs_c1", "demo", status="pr_open", pr_number=5, pr_node_id="PR_node", branch="apdl/x"
    )
    ready: list = []

    async def get_status(repo, ref, token):
        return "passed"

    async def mark_ready(**kwargs):
        ready.append(kwargs)

    result = await sync_ci_status(
        pool, "cs_c1", get_status=get_status, mint_token=_mint, mark_ready=mark_ready
    )

    assert result == "passed"
    final = await store.get_changeset(pool, "cs_c1")
    assert final.status == ChangesetStatus.ci_passed
    assert final.ci_status == "passed"
    assert ready and ready[0]["node_id"] == "PR_node"


@pytest.mark.asyncio
async def test_sync_no_ci_becomes_unverified_and_does_not_promote_ready():
    # A repo with no CI configured settles as explicitly unverified. The seeded
    # changeset's updated_at (_T0) is well outside the no-CI grace window, so
    # "none" is acted on rather than held as pending.
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset(
        "cs_none", "demo", status="pr_open", pr_number=5, pr_node_id="PR_n", branch="apdl/x"
    )
    ready: list = []

    async def get_status(repo, ref, token):
        return "none"

    async def mark_ready(**kwargs):
        ready.append(kwargs)

    result = await sync_ci_status(
        pool, "cs_none", get_status=get_status, mint_token=_mint, mark_ready=mark_ready
    )

    assert result == "unverified_external_ci"
    final = await store.get_changeset(pool, "cs_none")
    assert final.status == ChangesetStatus.unverified_external_ci
    assert final.ci_status == "unverified_external_ci"
    assert not ready


@pytest.mark.asyncio
async def test_sync_holds_none_as_pending_within_grace_window():
    # Right after a PR opens, commit-status-only CI (classic Travis/CircleCI) has
    # reported no status/check-suite/workflow yet, so get_ci_status returns "none"
    # even though CI is coming. Within the grace window we must NOT clear the gate:
    # hold as pending (ci_running), so a late status can still demote it.
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset(
        "cs_grace", "demo", status="pr_open", pr_number=5, pr_node_id="PR_g", branch="apdl/x"
    )
    pool.store["changesets"]["cs_grace"]["updated_at"] = datetime.now(timezone.utc)
    ready: list = []

    async def get_status(repo, ref, token):
        return "none"

    async def mark_ready(**kwargs):
        ready.append(kwargs)

    result = await sync_ci_status(
        pool, "cs_grace", get_status=get_status, mint_token=_mint, mark_ready=mark_ready
    )

    assert result == "pending"
    final = await store.get_changeset(pool, "cs_grace")
    assert final.status == ChangesetStatus.ci_running  # not advanced to ci_passed
    assert not ready  # PR not promoted ready


@pytest.mark.asyncio
async def test_sync_acts_on_none_after_grace_with_grace_disabled(monkeypatch):
    # With the grace window disabled (CODEGEN_CI_NONE_GRACE_SECONDS=0), a recent
    # changeset reporting "none" becomes unverified immediately.
    monkeypatch.setenv("CODEGEN_CI_NONE_GRACE_SECONDS", "0")
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset(
        "cs_now", "demo", status="pr_open", pr_number=5, pr_node_id="PR_now", branch="apdl/x"
    )
    pool.store["changesets"]["cs_now"]["updated_at"] = datetime.now(timezone.utc)

    async def get_status(repo, ref, token):
        return "none"

    result = await sync_ci_status(pool, "cs_now", get_status=get_status, mint_token=_mint)

    assert result == "unverified_external_ci"
    final = await store.get_changeset(pool, "cs_now")
    assert final.status == ChangesetStatus.unverified_external_ci
    assert final.ci_status == "unverified_external_ci"


@pytest.mark.asyncio
async def test_sync_marks_failed():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_c2", "demo", status="ci_running", pr_number=5, branch="apdl/x")

    async def get_status(repo, ref, token):
        return "failed"

    result = await sync_ci_status(pool, "cs_c2", get_status=get_status, mint_token=_mint)

    assert result == "failed"
    final = await store.get_changeset(pool, "cs_c2")
    assert final.status == ChangesetStatus.ci_failed


@pytest.mark.asyncio
async def test_new_head_can_demote_previous_pass_to_pending():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset(
        "cs_new_head",
        "demo",
        status="ci_passed",
        ci_status="passed",
        pr_number=5,
        branch="apdl/x",
    )

    async def get_status(repo, ref, token):
        return CIStatus("pending", observed=True, head_sha="new-head")

    result = await sync_ci_status(
        pool, "cs_new_head", get_status=get_status, mint_token=_mint
    )

    assert result == "pending"
    final = await store.get_changeset(pool, "cs_new_head")
    assert final.status is ChangesetStatus.ci_running
    assert final.ci_status == "pending"


@pytest.mark.asyncio
async def test_sync_is_noop_in_terminal_state():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_c3", "demo", status="merged", pr_number=5, branch="apdl/x")

    async def get_status(repo, ref, token):
        raise AssertionError("CI should not be queried for a terminal changeset")

    result = await sync_ci_status(pool, "cs_c3", get_status=get_status, mint_token=_mint)
    assert result is None


@pytest.mark.asyncio
async def test_sync_still_failed_writes_nothing(monkeypatch):
    # A ci_failed changeset whose CI still reports failed must not be bounced
    # ci_failed → ci_running → ci_failed: the bounce refreshes updated_at on
    # every poll, which defeats the poller's age cap
    # (CODEGEN_CI_SYNC_MAX_AGE_SECONDS) and re-polls a dead PR forever.
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset(
        "cs_ff", "demo", status="ci_failed", ci_status="failed", pr_number=5, branch="apdl/x"
    )

    async def get_status(repo, ref, token):
        return "failed"

    async def forbidden_set_ci_status(*args, **kwargs):
        raise AssertionError("still-failed sync must not transition the changeset")

    monkeypatch.setattr(ci_module.store, "set_ci_status", forbidden_set_ci_status)
    result = await sync_ci_status(pool, "cs_ff", get_status=get_status, mint_token=_mint)

    assert result == "failed"
    final = await store.get_changeset(pool, "cs_ff")
    assert final.status == ChangesetStatus.ci_failed


@pytest.mark.asyncio
async def test_sync_releases_inferred_pending_after_deadline(monkeypatch):
    # Inferred pending (phantom app check-suites / a workflow that never triggers
    # on PR branches) past the deadline: nothing was ever observed on the ref, so
    # observation settles as unverified instead of holding ci_running forever.
    monkeypatch.setenv("CODEGEN_CI_PENDING_TIMEOUT", "600")
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset(
        "cs_dead",
        "demo",
        status="ci_running",
        ci_status="pending",
        ci_awaiting_since=datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc),  # long ago
        pr_number=5,
        pr_node_id="PR_d",
        branch="apdl/x",
    )
    ready: list = []

    async def get_status(repo, ref, token):
        return CIStatus("pending", observed=False)

    async def mark_ready(**kwargs):
        ready.append(kwargs)

    result = await sync_ci_status(
        pool, "cs_dead", get_status=get_status, mint_token=_mint, mark_ready=mark_ready
    )

    assert result == "unverified_external_ci"
    final = await store.get_changeset(pool, "cs_dead")
    assert final.status == ChangesetStatus.unverified_external_ci
    assert final.ci_status == "unverified_external_ci"
    assert not ready


@pytest.mark.asyncio
async def test_sync_never_times_out_observed_pending(monkeypatch):
    # OBSERVED pending — real check runs / statuses executing on the ref — is
    # never released by the deadline, no matter how long it has been running.
    monkeypatch.setenv("CODEGEN_CI_PENDING_TIMEOUT", "600")
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset(
        "cs_slow",
        "demo",
        status="ci_running",
        ci_status="pending",
        ci_awaiting_since=datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc),  # long ago
        pr_number=5,
        branch="apdl/x",
    )

    async def get_status(repo, ref, token):
        return CIStatus("pending", observed=True)

    result = await sync_ci_status(pool, "cs_slow", get_status=get_status, mint_token=_mint)

    assert result == "pending"
    final = await store.get_changeset(pool, "cs_slow")
    assert final.status == ChangesetStatus.ci_running  # still waiting


@pytest.mark.asyncio
async def test_sync_holds_inferred_pending_within_deadline(monkeypatch):
    monkeypatch.setenv("CODEGEN_CI_PENDING_TIMEOUT", "600")
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset(
        "cs_young",
        "demo",
        status="ci_running",
        ci_status="pending",
        ci_awaiting_since=datetime.now(timezone.utc),  # just started awaiting
        pr_number=5,
        branch="apdl/x",
    )

    async def get_status(repo, ref, token):
        return CIStatus("pending", observed=False)

    result = await sync_ci_status(pool, "cs_young", get_status=get_status, mint_token=_mint)

    assert result == "pending"
    final = await store.get_changeset(pool, "cs_young")
    assert final.status == ChangesetStatus.ci_running


@pytest.mark.asyncio
async def test_sync_deadline_disabled_waits_forever(monkeypatch):
    # CODEGEN_CI_PENDING_TIMEOUT=0 restores the wait-forever behavior.
    monkeypatch.setenv("CODEGEN_CI_PENDING_TIMEOUT", "0")
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset(
        "cs_wait",
        "demo",
        status="ci_running",
        ci_status="pending",
        ci_awaiting_since=datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc),
        pr_number=5,
        branch="apdl/x",
    )

    async def get_status(repo, ref, token):
        return CIStatus("pending", observed=False)

    result = await sync_ci_status(pool, "cs_wait", get_status=get_status, mint_token=_mint)

    assert result == "pending"
    final = await store.get_changeset(pool, "cs_wait")
    assert final.status == ChangesetStatus.ci_running


@pytest.mark.asyncio
async def test_sync_deadline_anchors_on_ci_awaiting_since_not_updated_at(monkeypatch):
    # updated_at is refreshed by every transition (including the sync's own
    # writes), so it must not be the deadline clock: a changeset with a FRESH
    # updated_at but an old ci_awaiting_since has genuinely waited too long.
    monkeypatch.setenv("CODEGEN_CI_PENDING_TIMEOUT", "600")
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset(
        "cs_anchor",
        "demo",
        status="ci_running",
        ci_status="pending",
        ci_awaiting_since=datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc),  # long ago
        pr_number=5,
        branch="apdl/x",
    )
    pool.store["changesets"]["cs_anchor"]["updated_at"] = datetime.now(timezone.utc)

    async def get_status(repo, ref, token):
        return CIStatus("pending", observed=False)

    result = await sync_ci_status(pool, "cs_anchor", get_status=get_status, mint_token=_mint)

    assert result == "unverified_external_ci"
    final = await store.get_changeset(pool, "cs_anchor")
    assert final.status == ChangesetStatus.unverified_external_ci
