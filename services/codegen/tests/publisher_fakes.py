"""Controller publication fakes shared by job and repair tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from app.github.publisher import PreparedBranch, PublishedBranch


class FakeBranchPublisher:
    def __init__(self) -> None:
        self.prepare_calls: list[dict] = []
        self.push_calls: list[tuple[PreparedBranch, str]] = []

    @asynccontextmanager
    async def prepare(self, **kwargs):
        self.prepare_calls.append(dict(kwargs))
        prepared = PreparedBranch(
            repository=kwargs["repository"],
            branch=kwargs["branch"],
            base_sha=kwargs["expected_base_sha"],
            expected_remote_sha=kwargs["expected_remote_sha"],
            candidate_head_sha=kwargs["candidate_head_sha"],
            head_sha=kwargs["candidate_head_sha"],
            tree_sha=kwargs["candidate_tree_sha"],
            workspace=Path("/fake/controller-publication"),
        )
        yield prepared

    async def push(
        self,
        prepared: PreparedBranch,
        *,
        write_token: str,
    ) -> PublishedBranch:
        self.push_calls.append((prepared, write_token))
        return PublishedBranch(
            branch=prepared.branch,
            head_sha=prepared.candidate_head_sha,
        )
