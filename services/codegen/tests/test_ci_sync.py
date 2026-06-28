"""Tests for CI status sync (fake pool + injected status reader)."""

import pytest

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
async def test_sync_no_ci_advances_to_passed_and_unblocks_merge():
    # A repo with no CI configured reports "none": nothing to wait on, so the
    # changeset advances to ci_passed (recorded as ci_status="none") and the PR
    # is promoted ready — the Merge gate is cleared without any checks.
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

    assert result == "none"
    final = await store.get_changeset(pool, "cs_none")
    assert final.status == ChangesetStatus.ci_passed
    assert final.ci_status == "none"
    assert ready and ready[0]["node_id"] == "PR_n"


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
async def test_sync_is_noop_in_terminal_state():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_c3", "demo", status="merged", pr_number=5, branch="apdl/x")

    async def get_status(repo, ref, token):
        raise AssertionError("CI should not be queried for a terminal changeset")

    result = await sync_ci_status(pool, "cs_c3", get_status=get_status, mint_token=_mint)
    assert result is None
