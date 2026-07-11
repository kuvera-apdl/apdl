"""Tests for the changeset job runner (fake editor + fake pool, no network)."""

import asyncio

import pytest

from app.contracts.models import ContractBundle
from app.editor.base import EditRequest, EditResult
from app.editor.fake import FakeEditor
from app.github.pulls import PullRequest
from app.jobs.runner import run_changeset_job
from app.models.changeset import ChangesetStatus
from app.store import changesets as store
from tests.fakes import FakePool

_TASK = {
    "title": "Add dark mode",
    "spec": "Implement a dark-mode toggle.",
    "context": {},
    "constraints": ["keeps existing tests green"],
}


async def _mint(installation_id: int, repo: str) -> str:
    return "ghs_tok"


async def _seed(pool: FakePool, changeset_id: str, project_id: str = "demo", base="main"):
    await store.create_changeset(
        pool,
        changeset_id=changeset_id,
        project_id=project_id,
        run_id="run-1",
        base_branch=base,
        task=_TASK,
    )


@pytest.mark.asyncio
async def test_job_opens_draft_pr_on_success():
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets", installation_id=1)
    await _seed(pool, "cs_abc12345")

    editor = FakeEditor(EditResult(success=True, diff_stat={"files": 3}))
    calls: dict = {}

    async def open_pr(**kwargs) -> PullRequest:
        calls.update(kwargs)
        return PullRequest(url="https://github.com/acme/widgets/pull/9", number=9)

    await run_changeset_job(pool, "cs_abc12345", editor=editor, mint_token=_mint, open_pr=open_pr)

    final = await store.get_changeset(pool, "cs_abc12345")
    assert final.status == ChangesetStatus.pr_open
    assert final.pr_url.endswith("/pull/9")
    assert final.pr_number == 9
    assert final.branch.startswith("apdl/add-dark-mode-")
    assert final.diff_stat == {"files": 3}
    # The editor saw the resolved repo/branch + the minted token.
    assert isinstance(editor.last_request, EditRequest)
    assert editor.last_request.repo == "acme/widgets"
    assert editor.last_request.base_branch == "main"
    assert editor.last_request.token == "ghs_tok"
    # The PR was opened as a draft, on the right repo/base, with the token.
    assert calls["draft"] is True
    assert calls["repo"] == "acme/widgets"
    assert calls["base"] == "main"
    assert calls["token"] == "ghs_tok"


@pytest.mark.asyncio
async def test_job_marks_tests_failed_without_opening_pr():
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_fail0001")

    editor = FakeEditor(EditResult(success=False, error="tests red"))
    opened: list = []

    async def open_pr(**kwargs) -> PullRequest:
        opened.append(kwargs)
        return PullRequest(url="x", number=1)

    await run_changeset_job(pool, "cs_fail0001", editor=editor, mint_token=_mint, open_pr=open_pr)

    final = await store.get_changeset(pool, "cs_fail0001")
    assert final.status == ChangesetStatus.tests_failed
    assert final.error == "tests red"
    assert opened == []


@pytest.mark.asyncio
async def test_job_errors_when_connection_missing():
    pool = FakePool()  # no connection seeded for "ghost"
    await store.create_changeset(
        pool, changeset_id="cs_ghost001", project_id="ghost",
        run_id=None, base_branch=None, task=_TASK,
    )

    async def open_pr(**kwargs) -> PullRequest:
        raise AssertionError("PR should not be opened")

    await run_changeset_job(
        pool, "cs_ghost001", editor=FakeEditor(), mint_token=_mint, open_pr=open_pr
    )

    final = await store.get_changeset(pool, "cs_ghost001")
    assert final.status == ChangesetStatus.error


@pytest.mark.asyncio
async def test_job_errors_on_unexpected_editor_fault():
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_boom0001")

    class _BoomEditor:
        async def implement(self, request: EditRequest) -> EditResult:
            raise RuntimeError("kaboom")

    async def open_pr(**kwargs) -> PullRequest:
        raise AssertionError("PR should not be opened")

    await run_changeset_job(
        pool, "cs_boom0001", editor=_BoomEditor(), mint_token=_mint, open_pr=open_pr
    )

    final = await store.get_changeset(pool, "cs_boom0001")
    assert final.status == ChangesetStatus.error
    assert "kaboom" in (final.error or "")


@pytest.mark.asyncio
async def test_job_is_a_noop_for_unknown_changeset():
    async def open_pr(**kwargs) -> PullRequest:
        raise AssertionError("PR should not be opened")

    # Should not raise.
    await run_changeset_job(
        FakePool(), "cs_missing", editor=FakeEditor(), mint_token=_mint, open_pr=open_pr
    )


@pytest.mark.asyncio
async def test_job_blocks_on_pre_push_gate_violation():
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_secret01")

    editor = FakeEditor(
        EditResult(success=True, diff_stat={"files": 1}, diff_text="AKIAIOSFODNN7EXAMPLE")
    )
    opened: list = []

    async def open_pr(**kwargs) -> PullRequest:
        opened.append(kwargs)
        return PullRequest(url="x", number=1)

    await run_changeset_job(pool, "cs_secret01", editor=editor, mint_token=_mint, open_pr=open_pr)

    final = await store.get_changeset(pool, "cs_secret01")
    assert final.status == ChangesetStatus.tests_failed
    assert "gate" in (final.error or "").lower()
    assert opened == []


@pytest.mark.asyncio
async def test_job_backs_off_when_already_claimed():
    # The queued → cloning transition is the claim; a duplicate job (double
    # enqueue, concurrent replica) must not touch the winner's changeset.
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_claimed", "demo", status="cloning")

    editor = FakeEditor()

    async def open_pr(**kwargs) -> PullRequest:
        raise AssertionError("PR should not be opened")

    await run_changeset_job(
        pool, "cs_claimed", editor=editor, mint_token=_mint, open_pr=open_pr
    )

    final = await store.get_changeset(pool, "cs_claimed")
    assert final.status == ChangesetStatus.cloning  # winner's state untouched
    assert final.error is None
    assert editor.last_request is None  # the loser never ran the editor


@pytest.mark.asyncio
async def test_job_passes_connection_policy_and_revert_sha_to_the_editor():
    pool = FakePool()
    pool.add_connection(
        "demo", policy='{"test_cmd": "make ci", "gates": {"max_files": 5}}'
    )
    task = {**_TASK, "context": {"revert_sha": "cafebabe"}}
    await store.create_changeset(
        pool, changeset_id="cs_pol00001", project_id="demo",
        run_id=None, base_branch=None, task=task,
    )

    editor = FakeEditor(EditResult(success=True, diff_stat={"files": 1}))

    async def open_pr(**kwargs) -> PullRequest:
        return PullRequest(url="https://github.com/acme/widgets/pull/2", number=2)

    await run_changeset_job(
        pool, "cs_pol00001", editor=editor, mint_token=_mint, open_pr=open_pr
    )

    assert editor.last_request.test_cmd == "make ci"
    assert editor.last_request.gates_policy == {"max_files": 5}
    assert editor.last_request.revert_sha == "cafebabe"


@pytest.mark.asyncio
async def test_job_abandoned_when_automation_disabled(monkeypatch):
    monkeypatch.setenv("CODEGEN_KILL_SWITCH", "true")
    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_killed01")

    async def open_pr(**kwargs) -> PullRequest:
        raise AssertionError("PR should not be opened")

    await run_changeset_job(
        pool, "cs_killed01", editor=FakeEditor(), mint_token=_mint, open_pr=open_pr
    )

    final = await store.get_changeset(pool, "cs_killed01")
    assert final.status == ChangesetStatus.abandoned


class _BlockingEditor:
    """Blocks in implement() until released, to observe the concurrency slot."""

    def __init__(self) -> None:
        self.started = 0
        self.first_started = asyncio.Event()
        self.release = asyncio.Event()

    async def implement(self, request: EditRequest) -> EditResult:
        self.started += 1
        self.first_started.set()
        await self.release.wait()
        return EditResult(success=True, branch=request.branch, diff_stat={"files": 1})


@pytest.mark.asyncio
async def test_jobs_serialize_at_concurrency_one(monkeypatch):
    # Force a fresh slot bound to this loop; default concurrency is 1.
    import app.jobs.runner as runner_mod

    monkeypatch.setattr(runner_mod, "_job_semaphore", None)

    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets", installation_id=1)
    await _seed(pool, "cs_one")
    await _seed(pool, "cs_two")

    editor = _BlockingEditor()

    async def open_pr(**kwargs) -> PullRequest:
        return PullRequest(url="https://github.com/acme/widgets/pull/1", number=1)

    t1 = asyncio.create_task(
        run_changeset_job(pool, "cs_one", editor=editor, mint_token=_mint, open_pr=open_pr)
    )
    t2 = asyncio.create_task(
        run_changeset_job(pool, "cs_two", editor=editor, mint_token=_mint, open_pr=open_pr)
    )

    # First job reaches the editor; let the loop spin so the second would too if unbounded.
    await asyncio.wait_for(editor.first_started.wait(), timeout=1)
    await asyncio.sleep(0.05)

    # Only one job is in-flight; the other waits at the slot, still queued.
    assert editor.started == 1
    assert (await store.get_changeset(pool, "cs_one")).status == ChangesetStatus.editing
    assert (await store.get_changeset(pool, "cs_two")).status == ChangesetStatus.queued

    editor.release.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2)

    assert editor.started == 2
    assert (await store.get_changeset(pool, "cs_one")).status == ChangesetStatus.pr_open
    assert (await store.get_changeset(pool, "cs_two")).status == ChangesetStatus.pr_open


@pytest.mark.asyncio
async def test_job_persists_prompt_transcript_on_success_and_failure():
    """EditResult.prompts lands on the changeset row either way the edit ends."""
    transcript = [
        {"stage": "edit", "label": "Edit instruction (attempt 1)",
         "system": None, "user": "do the thing", "notes": None},
    ]

    async def open_pr(**kwargs) -> PullRequest:
        return PullRequest(url="https://github.com/acme/widgets/pull/9", number=9)

    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_prompt_ok")
    await _seed(pool, "cs_prompt_ko")

    editor = FakeEditor(EditResult(success=True, prompts=transcript))
    await run_changeset_job(pool, "cs_prompt_ok", editor=editor, mint_token=_mint, open_pr=open_pr)
    ok = await store.get_changeset(pool, "cs_prompt_ok")
    assert ok.status == ChangesetStatus.pr_open
    assert ok.prompts == transcript

    editor = FakeEditor(EditResult(success=False, error="tests red", prompts=transcript))
    await run_changeset_job(pool, "cs_prompt_ko", editor=editor, mint_token=_mint, open_pr=open_pr)
    ko = await store.get_changeset(pool, "cs_prompt_ko")
    assert ko.status == ChangesetStatus.tests_failed
    assert ko.prompts == transcript


@pytest.mark.asyncio
async def test_job_persists_contract_evidence_without_changing_ci_status():
    async def open_pr(**kwargs) -> PullRequest:
        return PullRequest(url="https://github.com/acme/widgets/pull/10", number=10)

    pool = FakePool()
    pool.add_connection("demo")
    await _seed(pool, "cs_contracts")
    bundle = ContractBundle()

    await run_changeset_job(
        pool,
        "cs_contracts",
        editor=FakeEditor(EditResult(success=True, contract_bundle=bundle)),
        mint_token=_mint,
        open_pr=open_pr,
    )

    stored = await store.get_changeset(pool, "cs_contracts")
    assert stored.contract_bundle == bundle
    assert stored.status is ChangesetStatus.pr_open
    assert stored.ci_status is None
