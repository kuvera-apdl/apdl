"""Read combined CI status for a git ref via the GitHub status + checks APIs."""

from __future__ import annotations

import logging

import httpx

from app.config import github_api_url
from app.github.client import gh_client, gh_headers

logger = logging.getLogger(__name__)

_FAIL_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}

#: Page size + page cap for the paginated Checks-API reads. GitHub defaults to
#: 30 per page; a repo with more check runs than one page would otherwise have
#: its later runs (possibly the failing ones) silently ignored. The cap bounds
#: a pathological repo at 1000 runs — far beyond anything a merge gate needs.
_PER_PAGE = 100
_MAX_PAGES = 10


class CIStatus(str):
    """A CI status (``"passed" | "failed" | "pending" | "none"``) with evidence.

    Subclasses ``str`` so every existing consumer (equality checks, ``in``
    tuples, the injected ``get_status`` seam, persistence of the raw value)
    keeps working unchanged. The extra ``observed`` attribute records HOW the
    verdict was reached:

    - ``observed=True`` — actual reports exist on the ref (commit statuses or
      check runs). A ``pending`` here is real CI executing; wait for it.
    - ``observed=False`` — the verdict was *inferred* from circumstantial
      evidence only (live check-suites, the repo having active workflows).
      An inferred ``pending`` is a guess that CI *might* report; the sync
      layer may time-box it (see ``jobs.ci``) instead of waiting forever.

    Plain strings (older callers, test fakes) read as observed via
    ``getattr(status, "observed", True)`` — the conservative default.
    """

    observed: bool = True

    def __new__(cls, value: str, *, observed: bool = True) -> CIStatus:
        self = super().__new__(cls, value)
        self.observed = observed
        return self


async def _paginated_list(
    c: httpx.AsyncClient, url: str, token: str, key: str
) -> list[dict]:
    """Collect ``key`` items across GitHub Link-header pages (bounded)."""
    items: list[dict] = []
    next_url: str | None = f"{url}?per_page={_PER_PAGE}"
    for _ in range(_MAX_PAGES):
        if next_url is None:
            break
        resp = await c.get(next_url, headers=gh_headers(token))
        resp.raise_for_status()
        items.extend(resp.json().get(key) or [])
        next_url = (resp.links.get("next") or {}).get("url")
    return items


async def get_ci_status(
    repo: str, ref: str, token: str, *, client: httpx.AsyncClient | None = None
) -> CIStatus:
    """Return ``"passed" | "failed" | "pending" | "none"`` for ``ref``'s checks.

    Combines the legacy commit-status rollup with the Checks API check-runs
    (paginated — a failure on a later page must fail the ref): any failure →
    ``failed``, even while sibling runs are still executing; otherwise anything
    still running → ``pending``; all green with at least one signal → ``passed``.
    These verdicts are *observed* (real reports on the ref).

    When ``ref`` has no signal at all, :func:`_no_signal_status` decides between
    an *inferred* ``pending`` (evidence that CI is configured and should report)
    and ``none`` (no CI to wait on — callers may treat this as "nothing blocks a
    merge" rather than holding forever).
    """
    base = github_api_url()
    async with gh_client(client) as c:
        status_resp = await c.get(
            f"{base}/repos/{repo}/commits/{ref}/status", headers=gh_headers(token)
        )
        status_resp.raise_for_status()
        combined = status_resp.json()
        check_runs = await _paginated_list(
            c, f"{base}/repos/{repo}/commits/{ref}/check-runs", token, "check_runs"
        )

    state = combined.get("state", "")
    total = combined.get("total_count", 0)

    if state in ("failure", "error"):
        return CIStatus("failed")
    # A red conclusion fails the ref even while sibling runs are still going —
    # the verdict cannot improve, and it must not hide behind a slower run.
    if any(
        run.get("status") == "completed" and run.get("conclusion") in _FAIL_CONCLUSIONS
        for run in check_runs
    ):
        return CIStatus("failed")
    if any(run.get("status") != "completed" for run in check_runs):
        return CIStatus("pending")
    if state == "pending" and total > 0:
        return CIStatus("pending")
    if total > 0 or check_runs:
        return CIStatus("passed")

    # No commit statuses and no check-runs on this ref: decide between "CI is
    # configured but hasn't reported yet" (inferred pending) and "no CI exists"
    # (none) — otherwise a no-CI repo sits in ci_running forever and blocks
    # merges nothing will ever green-light.
    return await _no_signal_status(base, repo, ref, token, client=client)


def _is_live_suite(suite: dict) -> bool:
    """Whether a check-suite is real evidence that CI is (about to be) running.

    GitHub auto-creates a check-suite for EVERY installed app with
    ``checks:write`` permission on every push — even apps that never run checks
    on this repo (Vercel, Railway, …). Those phantom suites sit ``queued`` with
    zero check runs *forever*, and counting them as "CI is coming" wedges the
    changeset in ``ci_running`` permanently. A suite is live evidence only when
    it has actually started (``in_progress``) or already owns check runs; a
    queued/requested suite with zero runs is presumed phantom — the sync layer's
    grace window covers the brief legitimate queued-before-first-run gap.
    """
    if suite.get("status") == "completed":
        return False
    if suite.get("status") == "in_progress":
        return True
    return (suite.get("latest_check_runs_count") or 0) > 0


async def _no_signal_status(
    base: str, repo: str, ref: str, token: str, *, client: httpx.AsyncClient | None
) -> CIStatus:
    """Resolve the no-signal case: inferred ``pending`` vs ``none``.

    Evidence that CI should report, in order of consultation:

    - a *live* check-suite on the ref (started, or owning check runs) — see
      :func:`_is_live_suite` for why merely-queued empty suites do NOT count;
    - at least one **active** Actions workflow (disabled workflows can never
      run, so they are not evidence).

    Either yields an inferred ``pending`` (``observed=False``) — a guess the
    sync layer may time-box, since e.g. an active deploy-on-main workflow never
    reports on PR branches. Neither yields ``none``.

    NB: commit-status-only CI (classic Travis/CircleCI via the statuses API)
    registers neither a suite nor a workflow until its first status post, so the
    window before that post still resolves to ``none`` here — the caller
    (``jobs.ci.sync_ci_status``) guards that race with a grace period before
    acting on ``none``.
    """
    async with gh_client(client) as c:
        suites = await _paginated_list(
            c, f"{base}/repos/{repo}/commits/{ref}/check-suites", token, "check_suites"
        )
        workflows = await _paginated_list(
            c, f"{base}/repos/{repo}/actions/workflows", token, "workflows"
        )

    live_suites = sum(1 for s in suites if _is_live_suite(s))
    phantom_suites = sum(
        1 for s in suites if s.get("status") != "completed" and not _is_live_suite(s)
    )
    active_workflows = sum(1 for w in workflows if w.get("state") == "active")

    if live_suites:
        logger.info(
            "No status/check-run signal yet for %s@%s but %d live check-suite(s) "
            "exist; holding as pending (inferred).",
            repo,
            ref,
            live_suites,
        )
        return CIStatus("pending", observed=False)

    if active_workflows:
        logger.info(
            "No CI signal yet for %s@%s but %d active workflow(s) exist; holding "
            "as pending (inferred).",
            repo,
            ref,
            active_workflows,
        )
        return CIStatus("pending", observed=False)

    logger.info(
        "No CI configured for %s@%s (no active workflows, no live check-suites%s, "
        "no statuses/checks); reporting 'none' so the changeset is not blocked on "
        "CI that will never run.",
        repo,
        ref,
        f", {phantom_suites} phantom suite(s) ignored" if phantom_suites else "",
    )
    return CIStatus("none", observed=False)
