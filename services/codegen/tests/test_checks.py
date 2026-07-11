"""Unit tests for the GitHub CI-status reader (httpx MockTransport)."""

import httpx
import pytest

from app.github.checks import CIStatus, get_ci_status


def _transport(
    state: str,
    total: int,
    check_runs: list[dict],
    *,
    workflows: list[dict] | None = None,
    check_suites: list[dict] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"state": state, "total_count": total})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": check_runs})
        if request.url.path.endswith("/check-suites"):
            return httpx.Response(200, json={"check_suites": check_suites or []})
        if request.url.path.endswith("/actions/workflows"):
            return httpx.Response(200, json={"workflows": workflows or []})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def _status(transport: httpx.MockTransport) -> CIStatus:
    async with httpx.AsyncClient(transport=transport) as client:
        return await get_ci_status("acme/widgets", "sha", "tok", client=client)


@pytest.mark.asyncio
async def test_passed_when_combined_status_success():
    status = await _status(_transport("success", 1, []))
    assert status == "passed"
    assert status.observed  # a real report on the ref, not an inference


@pytest.mark.asyncio
async def test_failed_when_combined_status_failure():
    assert await _status(_transport("failure", 1, [])) == "failed"


@pytest.mark.asyncio
async def test_pending_when_a_check_run_is_incomplete():
    runs = [{"status": "in_progress", "conclusion": None}]
    status = await _status(_transport("pending", 0, runs))
    assert status == "pending"
    assert status.observed  # real CI is executing — never subject to the deadline


@pytest.mark.asyncio
async def test_failed_when_a_check_run_failed():
    runs = [{"status": "completed", "conclusion": "failure"}]
    assert await _status(_transport("success", 1, runs)) == "failed"


@pytest.mark.asyncio
async def test_failed_check_includes_actionable_annotations():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(
                200, json={"state": "success", "total_count": 1, "sha": "head123"}
            )
        if request.url.path.endswith("/commits/sha/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "id": 42,
                            "name": "pytest",
                            "status": "completed",
                            "conclusion": "failure",
                            "details_url": "https://github.com/acme/widgets/actions/runs/1",
                        }
                    ]
                },
            )
        if request.url.path.endswith("/check-runs/42/annotations"):
            return httpx.Response(
                200,
                json=[
                    {
                        "path": "tests/test_api.py",
                        "start_line": 17,
                        "message": "expected 200, got 500",
                    }
                ],
            )
        return httpx.Response(404)

    status = await _status(httpx.MockTransport(handler))
    assert status == "failed"
    assert status.head_sha == "head123"
    assert "check:42" in status.failure_key
    assert "tests/test_api.py:17" in status.failure_summary
    assert "expected 200, got 500" in status.failure_summary


@pytest.mark.asyncio
async def test_failed_wins_over_a_sibling_still_running():
    # A red conclusion fails the ref even while another run is still executing —
    # the verdict cannot improve, and it must not hide behind the slower run.
    runs = [
        {"status": "in_progress", "conclusion": None},
        {"status": "completed", "conclusion": "failure"},
    ]
    assert await _status(_transport("pending", 0, runs)) == "failed"


@pytest.mark.asyncio
async def test_passed_when_all_check_runs_green():
    runs = [{"status": "completed", "conclusion": "success"}]
    assert await _status(_transport("", 0, runs)) == "passed"


@pytest.mark.asyncio
async def test_check_runs_are_paginated_so_a_late_failure_is_seen():
    # GitHub pages check-runs (default 30/page). A failing run past page one must
    # still fail the ref — an unpaginated read would report "passed" on red CI.
    page1 = [{"status": "completed", "conclusion": "success"} for _ in range(100)]
    page2 = [{"status": "completed", "conclusion": "failure"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"state": "success", "total_count": 1})
        if request.url.path.endswith("/check-runs"):
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json={"check_runs": page2})
            return httpx.Response(
                200,
                json={"check_runs": page1},
                headers={
                    "Link": (
                        "<https://api.github.com/repos/acme/widgets/commits/sha/"
                        'check-runs?per_page=100&page=2>; rel="next"'
                    )
                },
            )
        return httpx.Response(404)

    assert await _status(httpx.MockTransport(handler)) == "failed"


@pytest.mark.asyncio
async def test_none_when_no_signal_and_no_workflows():
    # No statuses, no check-runs, and the repo has zero Actions workflows → the
    # repo has no CI to wait on, so report "none" for sync to mark unverified.
    status = await _status(_transport("pending", 0, []))
    assert status == "none"
    assert not status.observed


@pytest.mark.asyncio
async def test_pending_when_no_signal_yet_but_active_workflows_exist():
    # No checks reported yet, but the repo HAS active workflows — they are most
    # likely queued (e.g. right after the PR opened), so hold as pending. The
    # verdict is inferred, so the sync layer's deadline applies (a workflow that
    # only triggers on main would otherwise hold the gate forever).
    status = await _status(
        _transport("pending", 0, [], workflows=[{"state": "active"}, {"state": "active"}])
    )
    assert status == "pending"
    assert not status.observed


@pytest.mark.asyncio
async def test_none_when_only_disabled_workflows_exist():
    # A disabled workflow can never run — it is not evidence that CI will report.
    status = await _status(
        _transport("pending", 0, [], workflows=[{"state": "disabled_manually"}])
    )
    assert status == "none"


@pytest.mark.asyncio
async def test_none_when_only_phantom_queued_suites_exist():
    # GitHub auto-creates a check-suite for EVERY installed checks:write app on
    # every push (Vercel, Railway, …) — apps that never run checks on this repo
    # leave suites sitting "queued" with zero runs FOREVER. Counting those as
    # "CI is coming" wedged changesets in ci_running permanently; they must be
    # ignored. (The sync grace window covers a real CI's brief queued gap.)
    suites = [
        {"status": "queued", "conclusion": None, "latest_check_runs_count": 0},
        {"status": "queued", "conclusion": None, "latest_check_runs_count": 0},
    ]
    status = await _status(_transport("pending", 0, [], check_suites=suites))
    assert status == "none"


@pytest.mark.asyncio
async def test_pending_when_a_suite_is_in_progress():
    # An in_progress suite means an app has actually started working on the ref —
    # real evidence, held as (inferred) pending.
    suites = [{"status": "in_progress", "conclusion": None, "latest_check_runs_count": 0}]
    status = await _status(_transport("pending", 0, [], check_suites=suites))
    assert status == "pending"
    assert not status.observed


@pytest.mark.asyncio
async def test_pending_when_a_queued_suite_owns_check_runs():
    # A queued suite that already owns check runs is live, not phantom.
    suites = [{"status": "queued", "conclusion": None, "latest_check_runs_count": 3}]
    status = await _status(_transport("pending", 0, [], check_suites=suites))
    assert status == "pending"


@pytest.mark.asyncio
async def test_none_when_only_completed_suites_and_no_workflows():
    # All check-suites are completed (with no surfaced check-runs/statuses) and the
    # repo has no workflows — nothing is pending, so "none" is correct.
    suites = [{"status": "completed", "conclusion": None, "latest_check_runs_count": 0}]
    status = await _status(_transport("", 0, [], check_suites=suites))
    assert status == "none"
