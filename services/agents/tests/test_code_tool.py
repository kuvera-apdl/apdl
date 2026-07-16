from typing import Any

import pytest

from app.tools import code


@pytest.mark.asyncio
async def test_open_changeset_posts_task(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_post(
        project_id: str, path: str, payload: dict[str, Any] | None = None
    ):
        captured["project_id"] = project_id
        captured["path"] = path
        captured["payload"] = payload
        return {"changeset_id": "cs_1", "status": "queued"}

    monkeypatch.setattr(code, "_post", fake_post)

    result = await code.open_changeset(
        project_id="demo",
        title="Add dark mode",
        spec="Implement a dark-mode toggle.",
        idempotency_key="agent-effect:command-1:changeset-1",
        run_id="run-1",
        constraints=["keeps tests green"],
    )

    assert result["changeset_id"] == "cs_1"
    assert captured["project_id"] == "demo"
    assert captured["path"] == "/v1/changesets"
    assert captured["payload"]["project_id"] == "demo"
    assert (
        captured["payload"]["idempotency_key"]
        == "agent-effect:command-1:changeset-1"
    )
    assert captured["payload"]["run_id"] == "run-1"
    assert captured["payload"]["task"]["title"] == "Add dark mode"
    assert captured["payload"]["task"]["constraints"] == ["keeps tests green"]


@pytest.mark.parametrize(
    "idempotency_key",
    ["", "contains whitespace", "-starts-with-punctuation", "x" * 201],
)
@pytest.mark.asyncio
async def test_open_changeset_rejects_noncanonical_idempotency_key(
    monkeypatch, idempotency_key
):
    async def unexpected_post(*_args, **_kwargs):
        raise AssertionError("invalid identity must be rejected before egress")

    monkeypatch.setattr(code, "_post", unexpected_post)

    with pytest.raises(ValueError, match="idempotency_key"):
        await code.open_changeset(
            project_id="demo",
            title="Add dark mode",
            spec="Implement a dark-mode toggle.",
            idempotency_key=idempotency_key,
        )


def test_derived_changeset_key_is_stable_and_bounded():
    first = code.derive_changeset_idempotency_key(
        "experiment-treatment", "run-1", "experiment with arbitrary identity"
    )
    second = code.derive_changeset_idempotency_key(
        "experiment-treatment", "run-1", "experiment with arbitrary identity"
    )

    assert first == second
    assert len(first) <= 200
    assert first != code.derive_changeset_idempotency_key(
        "experiment-treatment", "run-2", "experiment with arbitrary identity"
    )


@pytest.mark.asyncio
async def test_get_changeset(monkeypatch):
    async def fake_get(project_id: str, path: str):
        assert project_id == "demo"
        return {"changeset_id": path.rsplit("/", 1)[-1], "status": "pr_open"}

    monkeypatch.setattr(code, "_get", fake_get)

    result = await code.get_changeset("demo", "cs_9")
    assert result["changeset_id"] == "cs_9"
    assert result["status"] == "pr_open"


@pytest.mark.asyncio
async def test_revert_changeset_hits_endpoint(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_post(
        project_id: str, path: str, payload: dict[str, Any] | None = None
    ):
        assert project_id == "demo"
        captured["path"] = path
        return {"changeset_id": "cs_revert", "status": "queued"}

    monkeypatch.setattr(code, "_post", fake_post)

    result = await code.revert_changeset("demo", "cs_9")
    assert captured["path"] == "/v1/changesets/cs_9/revert"
    assert result["changeset_id"] == "cs_revert"


@pytest.mark.asyncio
async def test_http_calls_use_project_scoped_service_key(monkeypatch):
    seen: dict[str, Any] = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, path, *, params, headers):
            seen.update(path=path, params=params, headers=headers)
            return _Response()

    monkeypatch.setattr(code.httpx, "AsyncClient", lambda **_kwargs: _Client())
    monkeypatch.setenv(
        "APDL_SERVICE_API_KEYS",
        '{"demo":"proj_demo_0123456789abcdef"}',
    )

    await code._get("demo", "/v1/changesets", params={"project_id": "demo"})
    assert seen["headers"] == {"X-API-Key": "proj_demo_0123456789abcdef"}
