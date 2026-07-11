"""Bounded GitHub Actions artifact retrieval and safe ZIP inspection."""

from __future__ import annotations

import fnmatch
import hashlib
import io
import re
import stat
import zipfile
from pathlib import PurePosixPath

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import github_api_url
from app.github.client import gh_client, gh_headers
from app.runtime.models import (
    ArtifactFileEvidence,
    RuntimeArtifactExpectation,
    RuntimeArtifactObservation,
    RuntimeEvidenceStatus,
)

_PER_PAGE = 100
_MAX_PAGES = 5
_MAX_REDIRECTS = 3
_HEAD_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_TEXT_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
        r"(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\Z)",
        re.DOTALL,
    ),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(
        r"(?im)\b(?:authorization|proxy-authorization)\s*[:=]\s*"
        r"(?:bearer|basic)\s+[^\s,;\"']+"
    ),
    re.compile(r"(?im)\b(?:cookie|set-cookie)\s*:\s*[^\r\n]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}\b"
    ),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@[^\s]+"),
    re.compile(
        r"(?i)\b(?:aws_session_token|session_token|database_url|redis_url|"
        r"postgres_url)\b\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(
        r"(?i)(?:[?&]|\b)(?:access_token|api_key|token|password|secret)="
        r"[^&\s]+"
    ),
    re.compile(
        r"(?i)\b(token|password|secret|api[_-]?key)\b(\s*[:=]\s*)"
        r"([^\s,;]+)"
    ),
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GitHubArtifact(StrictModel):
    artifact_id: int = Field(ge=1)
    workflow_run_id: int = Field(ge=1)
    head_sha: str = Field(min_length=1)
    name: str
    size_in_bytes: int = Field(ge=0)
    archive_download_url: str = Field(min_length=1)
    expired: bool = False


class ArtifactSafetyError(ValueError):
    """Raised for unsafe, oversized, or malformed artifact archives."""


class StaleActionsHeadError(ValueError):
    """Raised when an Actions resource does not belong to the requested head."""


def _next_api_page(response: httpx.Response) -> str | None:
    next_url = (response.links.get("next") or {}).get("url")
    if next_url is None:
        return None
    target = httpx.URL(next_url)
    configured = httpx.URL(github_api_url())
    if (target.scheme, target.host, target.port) != (
        configured.scheme,
        configured.host,
        configured.port,
    ):
        raise ArtifactSafetyError(
            "GitHub pagination attempted to leave the configured API host"
        )
    return next_url


def _validate_head_sha(head_sha: str) -> None:
    if not _HEAD_PATTERN.fullmatch(head_sha):
        raise ValueError("head_sha contains invalid characters")


def redact_text(value: str) -> tuple[str, bool]:
    """Redact secret-shaped values from logs and text artifact excerpts."""
    redacted = False
    for pattern in _TEXT_SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)\\b(token"):
            value, count = pattern.subn(r"\1\2[REDACTED]", value)
        else:
            value, count = pattern.subn("[REDACTED]", value)
        redacted = redacted or count > 0
    return value, redacted


async def _download_bounded_archive(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    *,
    max_bytes: int,
) -> bytes:
    """Follow redirects without forwarding tokens and enforce a streaming cap."""
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
                content_length = response.headers.get("content-length")
                if (
                    content_length
                    and content_length.isdecimal()
                    and int(content_length) > max_bytes
                ):
                    raise ArtifactSafetyError(
                        "artifact archive exceeds compressed-size limit"
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > max_bytes:
                        raise ArtifactSafetyError(
                            "artifact archive exceeds compressed-size limit"
                        )
                    data.extend(chunk)
                return bytes(data)
            finally:
                await response.aclose()
        location = response.headers.get("location")
        if not location:
            await response.aclose()
            raise ArtifactSafetyError(
                "GitHub artifact redirect is missing Location"
            )
        next_url = str(response.url.join(location))
        await response.aclose()
        if httpx.URL(next_url).scheme != "https" and configured.scheme == "https":
            raise ArtifactSafetyError(
                "GitHub artifact redirect attempted an insecure download"
            )
        current = next_url
    raise ArtifactSafetyError("GitHub artifact redirect limit exceeded")


async def _assert_run_head(
    client: httpx.AsyncClient,
    repo: str,
    run_id: int,
    head_sha: str,
    token: str,
) -> None:
    response = await client.get(
        f"{github_api_url()}/repos/{repo}/actions/runs/{run_id}",
        headers=gh_headers(token),
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ArtifactSafetyError("GitHub workflow-run response must be an object")
    actual = str(data.get("head_sha") or "")
    if actual != head_sha:
        raise StaleActionsHeadError(
            f"workflow run {run_id} belongs to {actual or 'unknown head'}, not {head_sha}"
        )


async def list_run_artifacts(
    repo: str,
    run_id: int,
    head_sha: str,
    token: str,
    *,
    client: httpx.AsyncClient | None = None,
    max_pages: int = _MAX_PAGES,
) -> list[GitHubArtifact]:
    """List non-expired artifacts after verifying the workflow run's exact head."""
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    _validate_head_sha(head_sha)
    base = github_api_url()
    artifacts: list[GitHubArtifact] = []
    async with gh_client(client) as c:
        await _assert_run_head(c, repo, run_id, head_sha, token)
        next_url: str | None = (
            f"{base}/repos/{repo}/actions/runs/{run_id}/artifacts?per_page={_PER_PAGE}"
        )
        for _ in range(max_pages):
            if next_url is None:
                break
            response = await c.get(next_url, headers=gh_headers(token))
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(
                payload.get("artifacts", []), list
            ):
                raise ArtifactSafetyError(
                    "GitHub artifacts response must contain a list"
                )
            for item in payload.get("artifacts") or []:
                if not isinstance(item, dict):
                    raise ArtifactSafetyError(
                        "GitHub artifact entries must be objects"
                    )
                if item.get("expired"):
                    continue
                artifacts.append(
                    GitHubArtifact(
                        artifact_id=item["id"],
                        workflow_run_id=run_id,
                        head_sha=head_sha,
                        name=str(item.get("name") or ""),
                        size_in_bytes=int(item.get("size_in_bytes") or 0),
                        archive_download_url=str(
                            item.get("archive_download_url") or ""
                        ),
                        expired=False,
                    )
                )
            next_url = _next_api_page(response)
    return sorted(artifacts, key=lambda item: (item.name, item.artifact_id))


def _safe_member_path(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or any(character in normalized for character in ("\x00", "\r", "\n"))
    ):
        raise ArtifactSafetyError(f"unsafe artifact member path: {name!r}")
    return path.as_posix()


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == stat.S_IFLNK


def inspect_artifact_zip(
    archive: bytes,
    *,
    max_archive_bytes: int = 10_000_000,
    max_files: int = 100,
    max_file_bytes: int = 2_000_000,
    max_total_uncompressed_bytes: int = 10_000_000,
    excerpt_chars: int = 8000,
) -> list[ArtifactFileEvidence]:
    """Inspect a ZIP without extracting it to disk or trusting member metadata."""
    if (
        min(
            max_archive_bytes,
            max_files,
            max_file_bytes,
            max_total_uncompressed_bytes,
            excerpt_chars,
        )
        <= 0
    ):
        raise ValueError("artifact inspection budgets must be positive")
    if len(archive) > max_archive_bytes:
        raise ArtifactSafetyError("artifact archive exceeds compressed-size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            infos = sorted(
                (info for info in bundle.infolist() if not info.is_dir()),
                key=lambda info: info.filename,
            )
            if len(infos) > max_files:
                raise ArtifactSafetyError("artifact exceeds file-count limit")
            if sum(info.file_size for info in infos) > max_total_uncompressed_bytes:
                raise ArtifactSafetyError(
                    "artifact exceeds total uncompressed-size limit"
                )
            normalized_paths = [_safe_member_path(info.filename) for info in infos]
            if len(normalized_paths) != len(set(normalized_paths)):
                raise ArtifactSafetyError("artifact contains duplicate member paths")

            evidence: list[ArtifactFileEvidence] = []
            for info, path in zip(infos, normalized_paths, strict=True):
                if _is_symlink(info):
                    raise ArtifactSafetyError(
                        f"artifact symbolic links are not allowed: {path}"
                    )
                if info.flag_bits & 0x1:
                    raise ArtifactSafetyError(
                        f"encrypted artifact members are not allowed: {path}"
                    )
                if info.file_size > max_file_bytes:
                    raise ArtifactSafetyError(
                        f"artifact member exceeds size limit: {path}"
                    )
                with bundle.open(info) as member:
                    data = member.read(max_file_bytes + 1)
                if len(data) > max_file_bytes:
                    raise ArtifactSafetyError(
                        f"artifact member expanded beyond size limit: {path}"
                    )
                binary = b"\x00" in data
                excerpt: str | None = None
                redacted = False
                if not binary:
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        binary = True
                    else:
                        text, redacted = redact_text(text)
                        excerpt = text[:excerpt_chars]
                evidence.append(
                    ArtifactFileEvidence(
                        path=path,
                        content_sha256=hashlib.sha256(data).hexdigest(),
                        byte_count=len(data),
                        text_excerpt=excerpt,
                        redacted=redacted,
                        binary=binary,
                    )
                )
            return evidence
    except zipfile.BadZipFile as exc:
        raise ArtifactSafetyError("artifact is not a valid ZIP archive") from exc


async def download_artifact_observation(
    artifact: GitHubArtifact,
    expectation: RuntimeArtifactExpectation,
    token: str,
    *,
    client: httpx.AsyncClient | None = None,
    max_archive_bytes: int = 10_000_000,
) -> RuntimeArtifactObservation:
    """Download and inspect one expected artifact for its exact workflow head."""
    if artifact.name != expectation.artifact_name:
        raise ValueError(
            f"artifact {artifact.name!r} does not satisfy {expectation.artifact_name!r}"
        )
    if artifact.size_in_bytes > max_archive_bytes:
        raise ArtifactSafetyError("artifact metadata exceeds compressed-size limit")
    async with gh_client(client) as c:
        archive = await _download_bounded_archive(
            c,
            artifact.archive_download_url,
            token,
            max_bytes=max_archive_bytes,
        )
    files = inspect_artifact_zip(archive, max_archive_bytes=max_archive_bytes)
    matching_files = [
        file
        for file in files
        if any(fnmatch.fnmatch(file.path, pattern) for pattern in expectation.paths)
    ]
    if not matching_files:
        return RuntimeArtifactObservation(
            artifact_name=artifact.name,
            artifact_id=artifact.artifact_id,
            workflow_run_id=artifact.workflow_run_id,
            head_sha=artifact.head_sha,
            status=RuntimeEvidenceStatus.unverified,
            requirement_ids=expectation.requirement_ids,
            files=[],
            github_url=artifact.archive_download_url,
            unverified_reason=(
                "GitHub artifact contained no files matching the expected runtime "
                "evidence paths."
            ),
        )
    return RuntimeArtifactObservation(
        artifact_name=artifact.name,
        artifact_id=artifact.artifact_id,
        workflow_run_id=artifact.workflow_run_id,
        head_sha=artifact.head_sha,
        status=RuntimeEvidenceStatus.observed,
        requirement_ids=expectation.requirement_ids,
        files=matching_files,
        github_url=artifact.archive_download_url,
    )


def missing_artifact_observation(
    expectation: RuntimeArtifactExpectation,
    *,
    workflow_run_id: int,
    head_sha: str,
    github_url: str | None = None,
) -> RuntimeArtifactObservation:
    """Represent absence as unverified evidence, never as a CI conclusion."""
    return RuntimeArtifactObservation(
        artifact_name=expectation.artifact_name,
        artifact_id=None,
        workflow_run_id=workflow_run_id,
        head_sha=head_sha,
        status=RuntimeEvidenceStatus.unverified,
        requirement_ids=expectation.requirement_ids,
        files=[],
        github_url=github_url,
        unverified_reason="Expected GitHub Actions artifact was not present.",
    )
