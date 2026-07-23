"""Exact-head GitHub Actions run, job, and bounded log retrieval."""

from __future__ import annotations

import re
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import github_api_url
from app.github.artifacts import StaleActionsHeadError
from app.github.client import gh_client, gh_headers, github_json_pages
from app.safety.secrets import redact_secrets

_PER_PAGE = 100
_MAX_PAGES = 5
_MAX_REDIRECTS = 3
_HEAD_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionsWorkflowRun(StrictModel):
    schema_version: Literal["actions_workflow_run@1"] = "actions_workflow_run@1"
    run_id: int = Field(ge=1)
    name: str = Field(max_length=300)
    head_sha: str = Field(min_length=1)
    status: str
    conclusion: str | None = None
    run_attempt: int = Field(default=1, ge=1)
    html_url: str | None = Field(default=None, max_length=2000)


class ActionsJob(StrictModel):
    schema_version: Literal["actions_job@1"] = "actions_job@1"
    job_id: int = Field(ge=1)
    workflow_run_id: int = Field(ge=1)
    head_sha: str = Field(min_length=1)
    name: str = Field(max_length=300)
    status: str
    conclusion: str | None = None
    html_url: str | None = Field(default=None, max_length=2000)


class ActionsJobLog(StrictModel):
    schema_version: Literal["actions_job_log@1"] = "actions_job_log@1"
    job_id: int = Field(ge=1)
    workflow_run_id: int = Field(ge=1)
    head_sha: str = Field(min_length=1)
    text: str
    byte_count: int = Field(ge=0)
    truncated: bool = False
    redacted: bool = False


def _validate_head_sha(head_sha: str) -> None:
    if not _HEAD_PATTERN.fullmatch(head_sha):
        raise ValueError("head_sha contains invalid characters")


async def _exact_run(
    client: httpx.AsyncClient,
    repo: str,
    run_id: int,
    head_sha: str,
    token: str,
) -> dict:
    response = await client.get(
        f"{github_api_url()}/repos/{repo}/actions/runs/{run_id}",
        headers=gh_headers(token),
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("GitHub workflow-run response must be an object")
    actual = str(data.get("head_sha") or "")
    if actual != head_sha:
        raise StaleActionsHeadError(
            f"workflow run {run_id} belongs to {actual or 'unknown head'}, not {head_sha}"
        )
    return data


async def list_workflow_runs(
    repo: str,
    head_sha: str,
    token: str,
    *,
    client: httpx.AsyncClient | None = None,
    max_pages: int = _MAX_PAGES,
) -> list[ActionsWorkflowRun]:
    """List only runs whose payload confirms the requested exact head SHA."""
    _validate_head_sha(head_sha)
    if max_pages <= 0:
        raise ValueError("head_sha and a positive max_pages are required")
    base = github_api_url()
    runs: list[ActionsWorkflowRun] = []
    url = (
        f"{base}/repos/{repo}/actions/runs?head_sha={head_sha}&per_page={_PER_PAGE}"
    )
    async with gh_client(client) as c:
        async for payload in github_json_pages(
            c, url, token, max_pages=max_pages
        ):
            if not isinstance(payload.get("workflow_runs", []), list):
                raise ValueError("GitHub workflow-runs response must contain a list")
            for item in payload.get("workflow_runs") or []:
                if not isinstance(item, dict):
                    raise ValueError("GitHub workflow-run entries must be objects")
                if str(item.get("head_sha") or "") != head_sha:
                    continue
                runs.append(
                    ActionsWorkflowRun(
                        run_id=item["id"],
                        name=str(item.get("name") or "workflow"),
                        head_sha=head_sha,
                        status=str(item.get("status") or "unknown"),
                        conclusion=item.get("conclusion"),
                        run_attempt=int(item.get("run_attempt") or 1),
                        html_url=item.get("html_url"),
                    )
                )
    return sorted(runs, key=lambda item: (item.run_id, item.run_attempt))


async def list_run_jobs(
    repo: str,
    run_id: int,
    head_sha: str,
    token: str,
    *,
    client: httpx.AsyncClient | None = None,
    max_pages: int = _MAX_PAGES,
) -> list[ActionsJob]:
    """List jobs only after re-checking that their workflow run is current-head."""
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    _validate_head_sha(head_sha)
    base = github_api_url()
    jobs: list[ActionsJob] = []
    async with gh_client(client) as c:
        await _exact_run(c, repo, run_id, head_sha, token)
        url = (
            f"{base}/repos/{repo}/actions/runs/{run_id}/jobs"
            f"?filter=latest&per_page={_PER_PAGE}"
        )
        async for payload in github_json_pages(
            c, url, token, max_pages=max_pages
        ):
            if not isinstance(payload.get("jobs", []), list):
                raise ValueError("GitHub workflow-jobs response must contain a list")
            for item in payload.get("jobs") or []:
                if not isinstance(item, dict):
                    raise ValueError("GitHub workflow-job entries must be objects")
                item_head = str(item.get("head_sha") or "")
                if item_head != head_sha:
                    continue
                jobs.append(
                    ActionsJob(
                        job_id=item["id"],
                        workflow_run_id=run_id,
                        head_sha=head_sha,
                        name=str(item.get("name") or "job"),
                        status=str(item.get("status") or "unknown"),
                        conclusion=item.get("conclusion"),
                        html_url=item.get("html_url"),
                    )
                )
    return sorted(jobs, key=lambda item: item.job_id)


async def _download_log_prefix(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    *,
    max_bytes: int,
) -> tuple[bytes, bool]:
    """Follow bounded redirects and stream only a redaction-safe log prefix."""
    current = url
    configured = httpx.URL(github_api_url())
    for _ in range(_MAX_REDIRECTS + 1):
        target = httpx.URL(current)
        is_api_origin = (target.scheme, target.host, target.port) == (
            configured.scheme,
            configured.host,
            configured.port,
        )
        headers = gh_headers(token) if is_api_origin else {}
        request = client.build_request("GET", current, headers=headers)
        response = await client.send(request, stream=True, follow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            try:
                response.raise_for_status()
                hard_limit = max_bytes + 512
                data = bytearray()
                truncated = False
                content_length = response.headers.get("content-length")
                if content_length and content_length.isdecimal():
                    truncated = int(content_length) > max_bytes
                async for chunk in response.aiter_bytes():
                    remaining = hard_limit + 1 - len(data)
                    if remaining <= 0:
                        truncated = True
                        break
                    data.extend(chunk[:remaining])
                    if len(chunk) > remaining or len(data) > hard_limit:
                        truncated = True
                        break
                truncated = truncated or len(data) > max_bytes
                return bytes(data[:hard_limit]), truncated
            finally:
                await response.aclose()
        location = response.headers.get("location")
        if not location:
            await response.aclose()
            raise ValueError("GitHub job-log redirect is missing Location")
        next_url = str(response.url.join(location))
        await response.aclose()
        if httpx.URL(next_url).scheme != "https" and configured.scheme == "https":
            raise ValueError("GitHub job-log redirect attempted an insecure download")
        current = next_url
    raise ValueError("GitHub job-log redirect limit exceeded")


async def read_job_log(
    repo: str,
    job_id: int,
    head_sha: str,
    token: str,
    *,
    client: httpx.AsyncClient | None = None,
    max_bytes: int = 20_000,
) -> ActionsJobLog:
    """Read a bounded, redacted log after verifying the job's exact head SHA."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    _validate_head_sha(head_sha)
    base = github_api_url()
    async with gh_client(client) as c:
        metadata_response = await c.get(
            f"{base}/repos/{repo}/actions/jobs/{job_id}",
            headers=gh_headers(token),
        )
        metadata_response.raise_for_status()
        metadata = metadata_response.json()
        if not isinstance(metadata, dict):
            raise ValueError("GitHub workflow-job response must be an object")
        actual = str(metadata.get("head_sha") or "")
        if actual != head_sha:
            raise StaleActionsHeadError(
                f"job {job_id} belongs to {actual or 'unknown head'}, not {head_sha}"
            )
        raw, truncated = await _download_log_prefix(
            c,
            f"{base}/repos/{repo}/actions/jobs/{job_id}/logs",
            token,
            max_bytes=max_bytes,
        )
    # Keep a small overlap past the output boundary so a token beginning at the
    # final visible byte is still recognized and redacted as a whole.
    inspected = raw[: max_bytes + 512]
    text = inspected.decode("utf-8", "replace")
    text, redacted = redact_secrets(text)
    text = text[:max_bytes]
    return ActionsJobLog(
        job_id=job_id,
        workflow_run_id=int(metadata["run_id"]),
        head_sha=head_sha,
        text=text,
        byte_count=min(len(raw), max_bytes),
        truncated=truncated,
        redacted=redacted,
    )
