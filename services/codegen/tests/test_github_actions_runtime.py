"""Exact-head GitHub Actions run/job/log retrieval tests."""

from __future__ import annotations

import httpx
import pytest

from app.github.actions import list_run_jobs, list_workflow_runs, read_job_log
from app.github.artifacts import StaleActionsHeadError


@pytest.mark.asyncio
async def test_actions_runs_and_jobs_are_filtered_and_guarded_by_exact_head():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/actions/runs"):
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 7,
                            "name": "CI",
                            "head_sha": "head-new",
                            "status": "completed",
                            "conclusion": "success",
                            "run_attempt": 1,
                            "html_url": "https://github.test/runs/7",
                        },
                        {
                            "id": 6,
                            "name": "CI",
                            "head_sha": "head-old",
                            "status": "completed",
                            "conclusion": "failure",
                        },
                    ]
                },
            )
        if path.endswith("/actions/runs/7"):
            return httpx.Response(200, json={"id": 7, "head_sha": "head-new"})
        if path.endswith("/actions/runs/7/jobs"):
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 70,
                            "head_sha": "head-new",
                            "name": "pytest",
                            "status": "completed",
                            "conclusion": "success",
                        },
                        {
                            "id": 60,
                            "head_sha": "head-old",
                            "name": "stale",
                            "status": "completed",
                            "conclusion": "failure",
                        },
                        {
                            "id": 50,
                            "name": "missing exact head",
                            "status": "completed",
                            "conclusion": "success",
                        },
                    ]
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runs = await list_workflow_runs(
            "acme/widgets", "head-new", "tok", client=client
        )
        jobs = await list_run_jobs("acme/widgets", 7, "head-new", "tok", client=client)

    assert [run.run_id for run in runs] == [7]
    assert [job.job_id for job in jobs] == [70]
    assert jobs[0].head_sha == "head-new"


@pytest.mark.asyncio
async def test_actions_reject_malformed_collection_payloads():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": {"id": 7}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="must contain a list"):
            await list_workflow_runs(
                "acme/widgets", "head-new", "tok", client=client
            )


@pytest.mark.asyncio
async def test_stale_workflow_run_is_rejected_before_job_read():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/actions/runs/7"):
            return httpx.Response(200, json={"id": 7, "head_sha": "head-old"})
        raise AssertionError("jobs endpoint must not be read for a stale run")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(StaleActionsHeadError):
            await list_run_jobs("acme/widgets", 7, "head-new", "tok", client=client)


@pytest.mark.asyncio
async def test_job_logs_are_bounded_redacted_and_cross_host_redirects_drop_token():
    token_seen_on_download: list[str | None] = []
    secret = "ghp_" + "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/actions/jobs/70"):
            return httpx.Response(
                200, json={"id": 70, "run_id": 7, "head_sha": "head-new"}
            )
        if path.endswith("/actions/jobs/70/logs"):
            return httpx.Response(
                302, headers={"Location": "https://objects.example/job-70.log"}
            )
        if request.url.host == "objects.example":
            token_seen_on_download.append(request.headers.get("Authorization"))
            return httpx.Response(
                200, content=(f"token={secret}\n" + "x" * 300).encode()
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        log = await read_job_log(
            "acme/widgets",
            70,
            "head-new",
            "tok",
            client=client,
            max_bytes=100,
        )

    assert log.truncated is True
    assert log.redacted is True
    assert secret not in log.text
    assert "[REDACTED]" in log.text
    assert token_seen_on_download == [None]
