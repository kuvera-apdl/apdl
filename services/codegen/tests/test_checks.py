"""Unit tests for the GitHub CI-status reader (httpx MockTransport)."""

import httpx
import pytest

from app.github.checks import get_ci_status


def _transport(
    state: str, total: int, check_runs: list[dict], *, workflow_count: int = 0
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"state": state, "total_count": total})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": check_runs})
        if request.url.path.endswith("/actions/workflows"):
            return httpx.Response(200, json={"total_count": workflow_count})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def _status(transport: httpx.MockTransport) -> str:
    async with httpx.AsyncClient(transport=transport) as client:
        return await get_ci_status("acme/widgets", "sha", "tok", client=client)


@pytest.mark.asyncio
async def test_passed_when_combined_status_success():
    assert await _status(_transport("success", 1, [])) == "passed"


@pytest.mark.asyncio
async def test_failed_when_combined_status_failure():
    assert await _status(_transport("failure", 1, [])) == "failed"


@pytest.mark.asyncio
async def test_pending_when_a_check_run_is_incomplete():
    runs = [{"status": "in_progress", "conclusion": None}]
    assert await _status(_transport("pending", 0, runs)) == "pending"


@pytest.mark.asyncio
async def test_failed_when_a_check_run_failed():
    runs = [{"status": "completed", "conclusion": "failure"}]
    assert await _status(_transport("success", 1, runs)) == "failed"


@pytest.mark.asyncio
async def test_passed_when_all_check_runs_green():
    runs = [{"status": "completed", "conclusion": "success"}]
    assert await _status(_transport("", 0, runs)) == "passed"


@pytest.mark.asyncio
async def test_none_when_no_signal_and_no_workflows():
    # No statuses, no check-runs, and the repo has zero Actions workflows → the
    # repo has no CI to wait on, so report "none" (merge is not blocked on CI).
    assert await _status(_transport("pending", 0, [], workflow_count=0)) == "none"


@pytest.mark.asyncio
async def test_pending_when_no_signal_yet_but_workflows_exist():
    # No checks reported yet, but the repo HAS workflows — they are most likely
    # queued (e.g. right after the PR opened), so hold as pending, not "none".
    assert await _status(_transport("pending", 0, [], workflow_count=2)) == "pending"
