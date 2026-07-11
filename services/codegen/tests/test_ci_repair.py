"""Bounded, deduplicated repairs run on the existing GitHub PR branch."""

import pytest

from app.editor.base import EditResult
from app.editor.fake import FakeEditor
from app.jobs.repair import repair_failed_ci
from app.models.changeset import CIRemediationStatus, ChangesetStatus
from app.store import changesets as store
from tests.fakes import FakePool


async def _mint(installation_id: int, repo: str) -> str:
    return "ghs_tok"


@pytest.mark.asyncio
async def test_ci_failure_pushes_repair_to_same_branch(monkeypatch):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "2")
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset(
        "cs_repair",
        "demo",
        status="ci_failed",
        ci_status="failed",
        branch="apdl/existing",
        pr_number=7,
    )
    editor = FakeEditor(
        EditResult(
            success=True,
            diff_stat={"files": 1},
            changed_paths=["src/fix.py"],
            diff_text="+fixed",
            head_sha="new-sha",
        )
    )

    await repair_failed_ci(
        pool,
        "cs_repair",
        "old-sha:check:1",
        "tests: assertion failed",
        editor=editor,
        mint_token=_mint,
    )

    final = await store.get_changeset(pool, "cs_repair")
    assert editor.last_request is not None
    assert editor.last_request.existing_branch is True
    assert editor.last_request.branch == "apdl/existing"
    assert "tests: assertion failed" in editor.last_request.spec
    assert final.status is ChangesetStatus.ci_running
    assert final.ci_status == "pending"
    assert final.ci_retry_count == 1
    assert final.ci_remediation_status is CIRemediationStatus.awaiting_ci


@pytest.mark.asyncio
async def test_failed_repairs_stop_at_configured_limit(monkeypatch):
    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "2")
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset(
        "cs_exhaust",
        "demo",
        status="ci_failed",
        ci_status="failed",
        branch="apdl/existing",
        pr_number=7,
    )
    editor = FakeEditor(EditResult(success=False, error="agent could not repair"))

    for _ in range(3):
        await repair_failed_ci(
            pool,
            "cs_exhaust",
            "old-sha:check:1",
            "tests failed",
            editor=editor,
            mint_token=_mint,
        )

    final = await store.get_changeset(pool, "cs_exhaust")
    assert final.status is ChangesetStatus.ci_failed
    assert final.ci_retry_count == 2
    assert final.ci_remediation_status is CIRemediationStatus.exhausted
    assert final.error == "agent could not repair"


@pytest.mark.asyncio
async def test_repair_completion_cannot_overwrite_github_merge():
    pool = FakePool()
    pool.add_changeset(
        "cs_race",
        "demo",
        status="merged",
        ci_status="passed",
        branch="apdl/existing",
        pr_number=7,
        merge_sha="merged-sha",
    )

    await store.finish_ci_repair(pool, "cs_race", success=True)

    final = await store.get_changeset(pool, "cs_race")
    assert final.status is ChangesetStatus.merged
    assert final.merge_sha == "merged-sha"
    assert final.ci_status == "passed"
