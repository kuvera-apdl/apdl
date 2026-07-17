"""GitHub pull-request creation/observation helpers (no merge capability)."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.github.pulls import (
    PullRequestDiscoveryError,
    PullRequestIdentityError,
    close_pull_request,
    find_pull_request_by_branch,
    get_pull_request,
    open_pull_request,
)
from app.models.observations import GitHubPRStatus


def _payload(
    number: int = 12,
    *,
    head: str = "apdl/add-x",
    head_sha: str = "a" * 40,
    base: str = "main",
    repository_id: int = 10,
    state: str = "open",
    merged: bool = False,
) -> dict:
    return {
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
        "number": number,
        "state": state,
        "draft": True,
        "merged_at": "2026-07-12T12:00:00Z" if merged else None,
        "head": {
            "ref": head,
            "sha": head_sha,
            "repo": {"id": repository_id},
        },
        "base": {"ref": base, "repo": {"id": repository_id}},
        "updated_at": "2026-07-11T12:00:00Z",
    }


@pytest.mark.asyncio
async def test_open_pull_request_posts_a_draft():
    captured: dict = {}
    accepted = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json=_payload())

    async def record(receipt):
        accepted.append(receipt)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        pr = await open_pull_request(
            repo="acme/widgets",
            repository_id=10,
            head="apdl/add-x",
            base="main",
            expected_head_sha="a" * 40,
            title="Add X",
            body="body",
            token="ghs_tok",
            on_accepted=record,
            client=client,
        )
    assert pr.number == 12
    assert pr.head_sha == "a" * 40
    assert pr.status is GitHubPRStatus.draft
    assert pr.github_updated_at == datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    assert accepted[0].pr_number == 12
    assert accepted[0].source == "create"
    assert captured["body"]["draft"] is True
    assert captured["url"].endswith("/repos/acme/widgets/pulls")


@pytest.mark.asyncio
async def test_open_journals_raw_identity_before_rejecting_a_mismatch():
    accepted = []

    async def record(receipt):
        accepted.append(receipt)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=_payload(head_sha="b" * 40))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PullRequestIdentityError, match="exact-head mismatch"):
            await open_pull_request(
                repo="acme/widgets",
                repository_id=10,
                head="apdl/add-x",
                base="main",
                expected_head_sha="a" * 40,
                title="Add X",
                body="body",
                token="ghs_tok",
                on_accepted=record,
                client=client,
            )

    assert accepted[0].pr_number == 12
    assert accepted[0].github_url.endswith("/pull/12")
    assert accepted[0].raw_response["head"]["sha"] == "b" * 40


@pytest.mark.asyncio
async def test_open_journals_malformed_accepted_json_without_derived_overflow():
    accepted = []
    raw_response = _payload(number=2_147_483_648)
    raw_response["html_url"] = "https://github.com/" + "x" * 3000

    async def record(receipt):
        accepted.append(receipt)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=raw_response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PullRequestIdentityError, match="no positive number"):
            await open_pull_request(
                repo="acme/widgets",
                repository_id=10,
                head="apdl/add-x",
                base="main",
                expected_head_sha="a" * 40,
                title="Add X",
                body="body",
                token="ghs_tok",
                on_accepted=record,
                client=client,
            )

    assert len(accepted) == 1
    assert accepted[0].pr_number is None
    assert accepted[0].github_url is None
    assert accepted[0].raw_response == raw_response


@pytest.mark.asyncio
async def test_open_rejects_phishing_pull_request_url_after_journaling():
    accepted = []
    payload = _payload()
    payload["html_url"] = "https://evil.example/acme/widgets/pull/12"

    async def record(receipt):
        accepted.append(receipt)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PullRequestIdentityError, match="URL"):
            await open_pull_request(
                repo="acme/widgets",
                repository_id=10,
                head="apdl/add-x",
                base="main",
                expected_head_sha="a" * 40,
                title="Add X",
                body="body",
                token="ghs_tok",
                on_accepted=record,
                client=client,
            )

    assert accepted[0].github_url is None
    assert accepted[0].raw_response == payload


@pytest.mark.asyncio
async def test_find_recovers_exact_pr_by_deterministic_branch():
    accepted = []
    captured = {}

    async def record(receipt):
        accepted.append(receipt)

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = dict(request.url.params)
        return httpx.Response(200, json=[_payload()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        pr = await find_pull_request_by_branch(
            repo="acme/widgets",
            repository_id=10,
            head="apdl/add-x",
            base="main",
            expected_head_sha="a" * 40,
            token="ghs_tok",
            on_accepted=record,
            client=client,
        )

    assert pr is not None and pr.number == 12
    assert accepted[0].source == "recovery"
    assert captured["query"]["head"] == "acme:apdl/add-x"
    assert captured["query"]["state"] == "all"


@pytest.mark.asyncio
async def test_find_retains_closed_pr_on_deterministic_branch():
    accepted = []

    async def record(receipt):
        accepted.append(receipt)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_payload(state="closed")])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        pr = await find_pull_request_by_branch(
            repo="acme/widgets",
            repository_id=10,
            head="apdl/add-x",
            base="main",
            expected_head_sha="a" * 40,
            token="ghs_tok",
            on_accepted=record,
            client=client,
        )

    assert pr is not None
    assert pr.status is GitHubPRStatus.closed
    assert accepted[0].pr_number == 12


@pytest.mark.asyncio
async def test_find_prefers_unique_live_pr_over_confirmed_closed_history():
    accepted = []

    async def record(receipt):
        accepted.append(receipt)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[_payload(41, state="closed"), _payload(42)],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        pr = await find_pull_request_by_branch(
            repo="acme/widgets",
            repository_id=10,
            head="apdl/add-x",
            base="main",
            expected_head_sha="a" * 40,
            token="ghs_tok",
            on_accepted=record,
            client=client,
        )

    assert pr is not None and pr.number == 42
    assert [receipt.pr_number for receipt in accepted] == [41, 42]


@pytest.mark.asyncio
async def test_find_fails_closed_when_branch_results_have_another_page():
    accepted = []

    async def record(receipt):
        accepted.append(receipt)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[_payload()],
            headers={
                "Link": (
                    "<https://api.github.com/repos/acme/widgets/pulls?"
                    'state=all&page=2>; rel="next"'
                )
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            PullRequestDiscoveryError,
            match="pagination is incomplete",
        ) as raised:
            await find_pull_request_by_branch(
                repo="acme/widgets",
                repository_id=10,
                head="apdl/add-x",
                base="main",
                expected_head_sha="a" * 40,
                token="ghs_tok",
                on_accepted=record,
                client=client,
            )

    assert [receipt.pr_number for receipt in accepted] == [12]
    assert [receipt.pr_number for receipt in raised.value.receipts] == [12]


@pytest.mark.asyncio
async def test_ambiguous_branch_recovery_retains_every_identity():
    accepted = []

    async def record(receipt):
        accepted.append(receipt)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_payload(12), _payload(13)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PullRequestDiscoveryError) as raised:
            await find_pull_request_by_branch(
                repo="acme/widgets",
                repository_id=10,
                head="apdl/add-x",
                base="main",
                expected_head_sha="a" * 40,
                token="ghs_tok",
                on_accepted=record,
                client=client,
            )

    assert [receipt.pr_number for receipt in raised.value.receipts] == [12, 13]
    assert [receipt.pr_number for receipt in accepted] == [12, 13]


@pytest.mark.asyncio
async def test_close_requires_exact_github_confirmation():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_payload())
        assert json.loads(request.content) == {"state": "closed"}
        return httpx.Response(200, json=_payload(state="closed"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await close_pull_request(
            repo="acme/widgets",
            repository_id=10,
            number=12,
            head="apdl/add-x",
            base="main",
            expected_head_sha="a" * 40,
            token="ghs_tok",
            client=client,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        _payload(number=99),
        _payload(head="apdl/not-the-deterministic-branch"),
        _payload(repository_id=11),
    ],
)
async def test_close_refuses_unvalidated_identity_before_patch(payload):
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PullRequestIdentityError):
            await close_pull_request(
                repo="acme/widgets",
                repository_id=10,
                number=12,
                head="apdl/add-x",
                base="main",
                expected_head_sha="a" * 40,
                token="ghs_tok",
                client=client,
            )

    assert methods == ["GET"]


@pytest.mark.asyncio
async def test_close_allows_head_mismatch_after_branch_ownership_validation():
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        state = "closed" if request.method == "PATCH" else "open"
        return httpx.Response(
            200,
            json=_payload(head_sha="b" * 40, state=state),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await close_pull_request(
            repo="acme/widgets",
            repository_id=10,
            number=12,
            head="apdl/add-x",
            base="main",
            expected_head_sha="a" * 40,
            token="ghs_tok",
            client=client,
        )

    assert methods == ["GET", "PATCH"]


@pytest.mark.asyncio
async def test_get_pull_request_reads_live_github_state():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "number": 12,
                "state": "open",
                "draft": False,
                "head": {"sha": "abc123"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        data = await get_pull_request("acme/widgets", 12, "ghs_tok", client=client)
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/repos/acme/widgets/pulls/12")
    assert data["head"]["sha"] == "abc123"
