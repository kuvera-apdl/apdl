from typing import Any

import pytest

from app.tools import code


@pytest.mark.asyncio
async def test_open_changeset_posts_task(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_post(path: str, payload: dict[str, Any] | None = None):
        captured["path"] = path
        captured["payload"] = payload
        return {"changeset_id": "cs_1", "status": "queued"}

    monkeypatch.setattr(code, "_post", fake_post)

    result = await code.open_changeset(
        project_id="demo",
        title="Add dark mode",
        spec="Implement a dark-mode toggle.",
        run_id="run-1",
        constraints=["keeps tests green"],
    )

    assert result["changeset_id"] == "cs_1"
    assert captured["path"] == "/v1/changesets"
    assert captured["payload"]["project_id"] == "demo"
    assert captured["payload"]["run_id"] == "run-1"
    assert captured["payload"]["task"]["title"] == "Add dark mode"
    assert captured["payload"]["task"]["constraints"] == ["keeps tests green"]


@pytest.mark.asyncio
async def test_get_changeset(monkeypatch):
    async def fake_get(path: str):
        return {"changeset_id": path.rsplit("/", 1)[-1], "status": "pr_open"}

    monkeypatch.setattr(code, "_get", fake_get)

    result = await code.get_changeset("cs_9")
    assert result["changeset_id"] == "cs_9"
    assert result["status"] == "pr_open"


@pytest.mark.asyncio
async def test_revert_changeset_hits_endpoint(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_post(path: str, payload: dict[str, Any] | None = None):
        captured["path"] = path
        return {"changeset_id": "cs_revert", "status": "queued"}

    monkeypatch.setattr(code, "_post", fake_post)

    result = await code.revert_changeset("cs_9")
    assert captured["path"] == "/v1/changesets/cs_9/revert"
    assert result["changeset_id"] == "cs_revert"


def test_headers_carry_internal_token(monkeypatch):
    monkeypatch.setenv("APDL_INTERNAL_TOKEN", "s3cret")
    assert code._headers() == {"X-APDL-Internal-Token": "s3cret"}
    monkeypatch.delenv("APDL_INTERNAL_TOKEN", raising=False)
    assert code._headers() == {}
