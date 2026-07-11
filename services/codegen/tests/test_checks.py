"""Tests for bounded raw GitHub CI evidence collection on an exact head."""

from __future__ import annotations

import httpx
import pytest

from app.github.checks import GitHubCIEvidence, get_ci_evidence


async def _evidence(transport: httpx.MockTransport) -> GitHubCIEvidence:
    async with httpx.AsyncClient(transport=transport) as client:
        return await get_ci_evidence("acme/widgets", "head-exact", "tok", client=client)


@pytest.mark.asyncio
async def test_reads_only_raw_status_and_check_runs_for_the_exact_head():
    requested: list[str] = []
    combined = {
        "sha": "head-exact",
        "state": "pending",
        "total_count": 0,
        "statuses": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer tok"
        if request.url.path.endswith("/commits/head-exact/status"):
            return httpx.Response(200, json=combined)
        if request.url.path.endswith("/commits/head-exact/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        raise AssertionError(f"unexpected inference request: {request.url}")

    evidence = await _evidence(httpx.MockTransport(handler))

    assert evidence.combined_status == combined
    assert evidence.check_runs == []
    assert requested == [
        "/repos/acme/widgets/commits/head-exact/status",
        "/repos/acme/widgets/commits/head-exact/check-runs",
    ]


@pytest.mark.asyncio
async def test_preserves_raw_signals_without_aggregating_a_ci_verdict():
    combined = {
        "sha": "head-exact",
        "state": "failure",
        "total_count": 1,
        "statuses": [
            {
                "id": 7,
                "sha": "head-exact",
                "context": "deploy",
                "state": "failure",
            }
        ],
    }
    run = {
        "id": 9,
        "head_sha": "head-exact",
        "name": "tests",
        "status": "in_progress",
        "conclusion": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json=combined)
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [run]})
        return httpx.Response(404)

    evidence = await _evidence(httpx.MockTransport(handler))

    assert evidence.combined_status == combined
    assert evidence.check_runs == [run]
    assert not hasattr(evidence, "status")


@pytest.mark.asyncio
async def test_check_runs_are_collected_across_bounded_github_pages():
    first = {
        "id": 1,
        "head_sha": "head-exact",
        "name": "lint",
        "status": "completed",
        "conclusion": "success",
    }
    second = {
        "id": 2,
        "head_sha": "head-exact",
        "name": "tests",
        "status": "completed",
        "conclusion": "success",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(
                200,
                json={"sha": "head-exact", "total_count": 0, "statuses": []},
            )
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json={"check_runs": [second]})
        return httpx.Response(
            200,
            json={"check_runs": [first]},
            headers={
                "Link": (
                    "<https://api.github.com/repos/acme/widgets/commits/"
                    'head-exact/check-runs?per_page=100&page=2>; rel="next"'
                )
            },
        )

    evidence = await _evidence(httpx.MockTransport(handler))

    assert [run["id"] for run in evidence.check_runs] == [1, 2]


@pytest.mark.asyncio
async def test_failed_check_annotations_are_bounded_and_attached_to_raw_runs():
    runs = [
        {
            "id": number,
            "head_sha": "head-exact",
            "name": f"job-{number}",
            "status": "completed",
            "conclusion": "failure",
        }
        for number in range(1, 12)
    ]
    annotation_requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(
                200,
                json={"sha": "head-exact", "total_count": 0, "statuses": []},
            )
        if request.url.path.endswith("/commits/head-exact/check-runs"):
            return httpx.Response(200, json={"check_runs": runs})
        if "/annotations" in request.url.path:
            run_id = int(request.url.path.split("/")[-2])
            annotation_requests.append(run_id)
            assert request.url.params["per_page"] == "50"
            return httpx.Response(
                200,
                json=[
                    {
                        "path": "tests/test_api.py",
                        "start_line": number,
                        "annotation_level": "failure",
                        "message": f"failure {number}",
                    }
                    for number in range(1, 61)
                ],
            )
        return httpx.Response(404)

    evidence = await _evidence(httpx.MockTransport(handler))

    assert annotation_requests == list(range(1, 11))
    assert len(evidence.check_runs[0]["_failure_annotations"]) == 50
    assert "_failure_annotations" not in evidence.check_runs[10]


@pytest.mark.asyncio
async def test_annotation_lookup_failure_keeps_the_raw_failed_run():
    run = {
        "id": 42,
        "head_sha": "head-exact",
        "name": "tests",
        "status": "completed",
        "conclusion": "timed_out",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"sha": "head-exact", "statuses": []})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [run]})
        if request.url.path.endswith("/annotations"):
            return httpx.Response(503)
        return httpx.Response(404)

    evidence = await _evidence(httpx.MockTransport(handler))

    assert evidence.check_runs == [run]
    assert "_failure_annotations" not in evidence.check_runs[0]


@pytest.mark.asyncio
async def test_rejects_a_non_object_combined_status_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json=[])
        raise AssertionError("check runs must not be fetched after invalid status data")

    with pytest.raises(ValueError, match="must be an object"):
        await _evidence(httpx.MockTransport(handler))
