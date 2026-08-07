from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.service_auth import CAPABILITY_HEADER
from app.tools import code
from tests.capability_helpers import (
    make_mutation_capability,
    make_service_capability,
)


@pytest.mark.asyncio
async def test_open_changeset_posts_task(monkeypatch):
    captured: dict[str, Any] = {}
    capability = make_mutation_capability(run_id="run-1")

    async def fake_post(
        received_capability,
        project_id: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ):
        captured["capability"] = received_capability
        captured["project_id"] = project_id
        captured["path"] = path
        captured["payload"] = payload
        return {"changeset_id": "cs_1", "status": "queued"}

    monkeypatch.setattr(code, "_post", fake_post)

    result = await code.open_changeset(
        capability,
        project_id="demo",
        title="Add dark mode",
        spec="Implement a dark-mode toggle.",
        idempotency_key="agent-effect:command-1:changeset-1",
        run_id="run-1",
        constraints=["keeps tests green"],
    )

    assert result["changeset_id"] == "cs_1"
    assert captured["capability"] is capability
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


@pytest.mark.asyncio
async def test_changeset_capability_is_project_scoped_and_strict(monkeypatch):
    seen: dict[str, Any] = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "project_id": "demo",
                "changeset_creation": "available",
                "reasons": [],
                "checks": dict.fromkeys(code._CAPABILITY_CHECKS, "ready"),
            }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, path, *, params, headers):
            seen.update(path=path, params=params, headers=headers)
            return _Response()

    monkeypatch.setattr(code.httpx, "AsyncClient", lambda **_kwargs: _Client())
    delegated_headers = {"X-API-Key": "proj_demo_0123456789abcdef"}

    assert (
        await code.get_changeset_creation_capability("demo", delegated_headers)
        == "available"
    )
    assert seen["path"] == "/v1/capabilities/changeset-creation"
    assert seen["params"] == {"project_id": "demo"}
    assert seen["headers"] == delegated_headers


@pytest.mark.asyncio
async def test_changeset_capability_requires_exact_delegated_human_header(monkeypatch):
    def unexpected_client(**_kwargs):
        pytest.fail("invalid delegated authority reached transport")

    monkeypatch.setattr(code.httpx, "AsyncClient", unexpected_client)

    with pytest.raises(ValueError, match="one delegated API key"):
        await code.get_changeset_creation_capability(
            "demo",
            {
                "X-API-Key": "proj_demo_0123456789abcdef",
                CAPABILITY_HEADER: f"apdlcap_{'A' * 43}",
            },
        )


def _patch_capability_response(monkeypatch, payload):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, path, *, params, headers):
            assert path == "/v1/capabilities/changeset-creation"
            assert params == {"project_id": "demo"}
            assert set(headers) == {"X-API-Key"}
            return _Response()

    monkeypatch.setattr(code.httpx, "AsyncClient", lambda **_kwargs: _Client())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"project_id": "other"},
        {"unknown": True},
        {"changeset_creation": "available", "reasons": ["runtime_unavailable"]},
    ],
)
async def test_changeset_capability_rejects_ambiguous_responses(
    mutation,
    monkeypatch,
):
    payload = {
        "project_id": "demo",
        "changeset_creation": "disabled",
        "reasons": ["runtime_unavailable"],
        "checks": {
            **dict.fromkeys(code._CAPABILITY_CHECKS, "ready"),
            "runtime": "blocked",
        },
    }
    payload.update(mutation)

    _patch_capability_response(monkeypatch, payload)

    with pytest.raises(ValueError, match="capability response"):
        await code.get_changeset_creation_capability(
            "demo",
            {"X-API-Key": "proj_demo_0123456789abcdef"},
        )


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
            make_mutation_capability(run_id="run-1"),
            project_id="demo",
            title="Add dark mode",
            spec="Implement a dark-mode toggle.",
            idempotency_key=idempotency_key,
            run_id="run-1",
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
    capability = make_service_capability()

    async def fake_get(received_capability, project_id: str, path: str):
        assert received_capability is capability
        assert project_id == "demo"
        return {"changeset_id": path.rsplit("/", 1)[-1], "status": "pr_open"}

    monkeypatch.setattr(code, "_get", fake_get)

    result = await code.get_changeset(capability, "demo", "cs_9")
    assert result["changeset_id"] == "cs_9"
    assert result["status"] == "pr_open"


@pytest.mark.asyncio
async def test_revert_changeset_hits_endpoint(monkeypatch):
    captured: dict[str, Any] = {}
    capability = make_mutation_capability()

    async def fake_post(
        received_capability,
        project_id: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ):
        assert received_capability is capability
        assert project_id == "demo"
        captured["path"] = path
        return {"changeset_id": "cs_revert", "status": "queued"}

    monkeypatch.setattr(code, "_post", fake_post)

    result = await code.revert_changeset(capability, "demo", "cs_9")
    assert captured["path"] == "/v1/changesets/cs_9/revert"
    assert result["changeset_id"] == "cs_revert"


@pytest.mark.asyncio
async def test_http_calls_use_run_scoped_internal_capability(monkeypatch):
    seen: dict[str, Any] = {}
    capability = make_service_capability()

    @asynccontextmanager
    async def fake_service_headers(context, *, audiences, roles):
        seen.update(context=context, audiences=audiences, roles=roles)
        yield {CAPABILITY_HEADER: f"apdlcap_{'C' * 43}"}

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
    monkeypatch.setattr(code, "service_headers", fake_service_headers)

    await code._get(
        capability,
        "demo",
        "/v1/changesets",
        params={"project_id": "demo"},
    )
    assert seen["context"] is capability
    assert seen["audiences"] == ("codegen",)
    assert seen["roles"] == ("agents:read",)
    assert seen["headers"] == {CAPABILITY_HEADER: f"apdlcap_{'C' * 43}"}


@pytest.mark.asyncio
async def test_codegen_mutation_binds_capability_to_exact_post(monkeypatch):
    seen: dict[str, Any] = {}
    capability = make_mutation_capability(project_id="demo")
    payload = {"project_id": "demo", "run_id": capability.run_id}

    @asynccontextmanager
    async def fake_service_headers(context, **kwargs):
        seen.update(context=context, **kwargs)
        yield {CAPABILITY_HEADER: f"apdlcap_{'C' * 43}"}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"changeset_id": "cs_1"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, path, *, json, headers):
            seen.update(path=path, payload=json, headers=headers)
            return _Response()

    monkeypatch.setattr(code, "service_headers", fake_service_headers)
    monkeypatch.setattr(code.httpx, "AsyncClient", lambda **_kwargs: _Client())

    await code._post(capability, "demo", "/v1/changesets", payload)

    assert seen["context"] is capability
    assert seen["audiences"] == ("codegen",)
    assert seen["roles"] == ("agents:manage",)
    assert seen["request_method"] == "POST"
    assert seen["request_path"] == "/v1/changesets"
    assert seen["request_json"] == payload
    assert seen["path"] == "/v1/changesets"
    assert seen["payload"] == payload


@pytest.mark.asyncio
async def test_codegen_tool_rejects_cross_project_capability_before_egress(monkeypatch):
    def unexpected_client(**_kwargs):
        pytest.fail("cross-project call reached transport")

    monkeypatch.setattr(code.httpx, "AsyncClient", unexpected_client)

    with pytest.raises(ValueError, match="must match capability project"):
        await code._get(
            make_service_capability(project_id="demo"),
            "other",
            "/v1/changesets",
        )
