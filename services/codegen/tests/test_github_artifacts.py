"""Safe GitHub artifact metadata, download, and ZIP-processing tests."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from app.github.artifacts import (
    ArtifactSafetyError,
    GitHubArtifact,
    StaleActionsHeadError,
    download_artifact_observation,
    inspect_artifact_zip,
    list_run_artifacts,
    missing_artifact_observation,
)
from app.runtime.models import (
    RuntimeArtifactExpectation,
    RuntimeEvidenceKind,
    RuntimeEvidenceStatus,
)


def _zip(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return output.getvalue()


def _expectation() -> RuntimeArtifactExpectation:
    return RuntimeArtifactExpectation(
        artifact_name="apdl-browser-evidence",
        evidence_kind=RuntimeEvidenceKind.browser_report,
        paths=["playwright-report/**"],
        requirement_ids=["REQ-001"],
    )


@pytest.mark.asyncio
async def test_artifact_listing_rechecks_run_head_and_ignores_expired_items():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/actions/runs/7"):
            return httpx.Response(200, json={"id": 7, "head_sha": "head-new"})
        if request.url.path.endswith("/actions/runs/7/artifacts"):
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {
                            "id": 9,
                            "name": "apdl-browser-evidence",
                            "size_in_bytes": 123,
                            "archive_download_url": "https://api.github.com/artifacts/9",
                            "expired": False,
                        },
                        {
                            "id": 8,
                            "name": "old",
                            "size_in_bytes": 12,
                            "archive_download_url": "https://api.github.com/artifacts/8",
                            "expired": True,
                        },
                    ]
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        artifacts = await list_run_artifacts(
            "acme/widgets", 7, "head-new", "tok", client=client
        )

    assert [artifact.artifact_id for artifact in artifacts] == [9]
    assert artifacts[0].head_sha == "head-new"


@pytest.mark.asyncio
async def test_artifact_listing_rejects_stale_workflow_head():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 7, "head_sha": "head-old"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(StaleActionsHeadError):
            await list_run_artifacts(
                "acme/widgets", 7, "head-new", "tok", client=client
            )


def test_zip_processing_is_bounded_and_redacts_text_without_decoding_binary():
    secret = "ghp_" + "a" * 40
    files = inspect_artifact_zip(
        _zip(
            {
                "apdl-runtime-evidence.json": f'{{"token":"{secret}"}}'.encode(),
                "screenshots/home.png": b"\x89PNG\x00binary",
            }
        )
    )

    text = next(item for item in files if item.path.endswith(".json"))
    image = next(item for item in files if item.path.endswith(".png"))
    assert text.redacted is True
    assert secret not in (text.text_excerpt or "")
    assert image.binary is True
    assert image.text_excerpt is None

    with pytest.raises(ArtifactSafetyError, match="unsafe artifact member path"):
        inspect_artifact_zip(_zip({"../escape.txt": b"no"}))
    with pytest.raises(ArtifactSafetyError, match="file-count"):
        inspect_artifact_zip(_zip({"a.txt": b"a", "b.txt": b"b"}), max_files=1)
    with pytest.raises(ArtifactSafetyError, match="size limit"):
        inspect_artifact_zip(_zip({"large.txt": b"x" * 20}), max_file_bytes=10)


@pytest.mark.parametrize(
    "secret",
    [
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefghijk.signature123",
        "DATABASE_URL=postgresql://user:password@db.internal/app",
        "AWS_SESSION_TOKEN=temporary-cloud-session-token-123456",
        "https://api.example/items?access_token=top-secret-value",
    ],
)
def test_runtime_trace_credentials_are_redacted(secret):
    [evidence] = inspect_artifact_zip(_zip({"trace.txt": secret.encode()}))

    assert evidence.redacted is True
    assert secret not in (evidence.text_excerpt or "")


@pytest.mark.asyncio
async def test_downloaded_and_missing_artifacts_are_distinct_observations():
    archive = _zip(
        {
            "playwright-report/index.html": b"passed",
            "unrelated/debug.txt": b"not part of the evidence contract",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(
                302, headers={"Location": "https://objects.example/artifact.zip"}
            )
        if request.url.host == "objects.example":
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    artifact = GitHubArtifact(
        artifact_id=9,
        workflow_run_id=7,
        head_sha="head-new",
        name="apdl-browser-evidence",
        size_in_bytes=len(archive),
        archive_download_url="https://api.github.com/artifacts/9/zip",
    )
    expectation = _expectation()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observed = await download_artifact_observation(
            artifact, expectation, "tok", client=client
        )
        wrong_paths = expectation.model_copy(
            update={"paths": ["screenshots/**"]}, deep=True
        )
        unmatched = await download_artifact_observation(
            artifact, wrong_paths, "tok", client=client
        )
    missing = missing_artifact_observation(
        expectation, workflow_run_id=7, head_sha="head-new"
    )

    assert observed.status is RuntimeEvidenceStatus.observed
    assert observed.files[0].path == "playwright-report/index.html"
    assert len(observed.files) == 1
    assert unmatched.status is RuntimeEvidenceStatus.unverified
    assert "matching" in (unmatched.unverified_reason or "")
    assert missing.status is RuntimeEvidenceStatus.unverified
    assert missing.unverified_reason
