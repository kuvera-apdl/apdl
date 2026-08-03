from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.service_auth import CAPABILITY_HEADER
from app.tools import flags
from tests.capability_helpers import (
    make_mutation_capability,
    make_service_capability,
)


@pytest.fixture(autouse=True)
def capability_authority(monkeypatch):
    calls = []

    @asynccontextmanager
    async def fake_service_headers(
        context,
        *,
        audiences,
        roles,
        request_method=None,
        request_path=None,
        request_json=None,
        idempotency_key=None,
    ):
        call = {"context": context, "audiences": audiences, "roles": roles}
        if request_method is not None:
            call.update(
                request_method=request_method,
                request_path=request_path,
                request_json=request_json,
                idempotency_key=idempotency_key,
            )
        calls.append(call)
        yield {CAPABILITY_HEADER: f"apdlcap_{'F' * 43}"}

    monkeypatch.setattr(flags, "service_headers", fake_service_headers)
    return calls


@pytest.mark.asyncio
async def test_create_flag_derives_enabled_from_active_state(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_post(
        _capability,
        project_id: str,
        path: str,
        payload: dict[str, Any],
        params: dict[str, Any] | None = None,
    ):
        captured["project_id"] = project_id
        captured["path"] = path
        captured["payload"] = payload
        captured["params"] = params
        return {"created": True, "flag": payload}

    monkeypatch.setattr(flags, "_post", fake_post)

    await flags.create_flag(
        make_mutation_capability(project_id="apdl"),
        project_id="apdl",
        key="checkout",
        name="Checkout",
        state="active",
    )

    assert captured["path"] == "/v1/admin/flags"
    assert captured["project_id"] == "apdl"
    assert captured["params"] == {"project_id": "apdl"}
    assert captured["payload"]["state"] == "active"
    assert captured["payload"]["enabled"] is True
    assert captured["payload"]["default_variant"] == "control"
    assert captured["payload"]["variants"] == [
        {"key": "control", "weight": 1},
        {"key": "treatment", "weight": 1},
    ]
    assert captured["payload"]["fallthrough"] == {
        "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
    }
    assert "default_value" not in captured["payload"]
    assert "value" not in captured["payload"]["fallthrough"]


@pytest.mark.asyncio
async def test_create_flag_derives_draft_state_from_disabled_flag(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_post(
        _capability,
        project_id: str,
        path: str,
        payload: dict[str, Any],
        params: dict[str, Any] | None = None,
    ):
        captured["payload"] = payload
        return {"created": True, "flag": payload}

    monkeypatch.setattr(flags, "_post", fake_post)

    await flags.create_flag(
        make_mutation_capability(project_id="apdl"),
        project_id="apdl",
        key="checkout",
        name="Checkout",
    )

    assert captured["payload"]["state"] == "draft"
    assert captured["payload"]["enabled"] is False


@pytest.mark.asyncio
async def test_create_flag_posts_canonical_variant_fields(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_post(
        _capability,
        project_id: str,
        path: str,
        payload: dict[str, Any],
        params: dict[str, Any] | None = None,
    ):
        captured["payload"] = payload
        return {"created": True, "flag": payload}

    monkeypatch.setattr(flags, "_post", fake_post)

    await flags.create_flag(
        make_mutation_capability(project_id="apdl"),
        project_id="apdl",
        key="checkout",
        name="Checkout",
        default_variant="control",
        variants=[
            {"key": "control", "weight": 2},
            {"key": "treatment", "weight": 1},
        ],
        rules=[],
        fallthrough={"rollout": {"percentage": 25.0, "bucket_by": "user_id"}},
    )

    assert captured["payload"]["default_variant"] == "control"
    assert captured["payload"]["variants"] == [
        {"key": "control", "weight": 2},
        {"key": "treatment", "weight": 1},
    ]
    assert captured["payload"]["fallthrough"] == {
        "rollout": {"percentage": 25.0, "bucket_by": "user_id"},
    }
    assert "default_value" not in captured["payload"]
    assert "value" not in captured["payload"]["fallthrough"]


@pytest.mark.asyncio
async def test_create_flag_preserves_explicit_empty_canonical_inputs(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_post(
        _capability,
        project_id: str,
        path: str,
        payload: dict[str, Any],
        params: dict[str, Any] | None = None,
    ):
        captured["payload"] = payload
        return {"created": True, "flag": payload}

    monkeypatch.setattr(flags, "_post", fake_post)

    await flags.create_flag(
        make_mutation_capability(project_id="apdl"),
        project_id="apdl",
        key="checkout",
        name="Checkout",
        variants=[],
        fallthrough={},
    )

    assert captured["payload"]["variants"] == []
    assert captured["payload"]["fallthrough"] == {}


@pytest.mark.asyncio
async def test_update_flag_posts_canonical_variant_updates(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_put(
        _capability,
        project_id: str,
        path: str,
        payload: dict[str, Any],
        params: dict[str, Any] | None = None,
    ):
        captured["project_id"] = project_id
        captured["path"] = path
        captured["payload"] = payload
        captured["params"] = params
        return {"updated": True, "flag": payload}

    monkeypatch.setattr(flags, "_put", fake_put)

    await flags.update_flag(
        make_mutation_capability(project_id="apdl"),
        project_id="apdl",
        key="checkout",
        version=3,
        default_variant="control",
        variants=[
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 2},
        ],
    )

    assert captured["path"] == "/v1/admin/flags/checkout"
    assert captured["project_id"] == "apdl"
    assert captured["params"] == {"project_id": "apdl"}
    assert captured["payload"] == {
        "version": 3,
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 2},
        ],
    }


@pytest.mark.asyncio
async def test_transition_flag_uses_dedicated_versioned_endpoint(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_post(_capability, project_id, path, payload, params=None):
        captured.update(
            project_id=project_id,
            path=path,
            payload=payload,
            params=params,
        )
        return {"updated": True}

    monkeypatch.setattr(flags, "_post", fake_post)

    await flags.transition_flag(
        make_mutation_capability(project_id="apdl"),
        "apdl",
        "checkout",
        version=3,
        target_state="active",
    )

    assert captured == {
        "project_id": "apdl",
        "path": "/v1/admin/flags/checkout/transition",
        "payload": {"version": 3, "target_state": "active"},
        "params": {"project_id": "apdl"},
    }


@pytest.mark.asyncio
async def test_disable_flag_sends_version_without_source_alias(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_post(_capability, project_id, path, payload, params=None):
        captured.update(path=path, payload=payload, params=params)
        return {"disabled": True}

    monkeypatch.setattr(flags, "_post", fake_post)

    await flags.disable_flag(
        make_mutation_capability(project_id="apdl"),
        "apdl",
        "checkout",
        version=4,
        evidence={"verdict": "rollback"},
    )

    assert captured == {
        "path": "/v1/admin/flags/checkout/disable",
        "payload": {
            "version": 4,
            "reason": "experiment_rollback",
            "evidence": {"verdict": "rollback"},
        },
        "params": {"project_id": "apdl"},
    }


@pytest.mark.asyncio
async def test_evaluate_gate_defaults_to_non_logging_request(
    monkeypatch,
    capability_authority,
):
    captured: dict[str, Any] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"reason": "fallthrough"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, path, *, json, headers):
            captured.update(path=path, payload=json, headers=headers)
            return FakeResponse()

    monkeypatch.setattr(
        flags.httpx,
        "AsyncClient",
        lambda **kwargs: FakeClient(),
    )

    capability = make_service_capability(project_id="apdl")
    await flags.evaluate_gate(
        capability,
        "apdl",
        "checkout",
        user_id="user-1",
    )

    assert captured["path"] == "/v1/evaluate"
    assert captured["payload"]["log_exposure"] is False
    assert captured["payload"]["message_id"] == ""
    assert captured["headers"] == {CAPABILITY_HEADER: f"apdlcap_{'F' * 43}"}
    assert capability_authority[-1] == {
        "context": capability,
        "audiences": ("config",),
        "roles": ("config:evaluate",),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", ["", "   ", " padded "])
async def test_evaluate_gate_rejects_invalid_logging_contract(
    monkeypatch,
    message_id,
):
    def client(**kwargs):
        pytest.fail("invalid request reached transport")

    monkeypatch.setattr(flags.httpx, "AsyncClient", client)

    with pytest.raises(
        ValueError,
        match="log_exposure requires a stable nonblank message_id",
    ):
        await flags.evaluate_gate(
            make_service_capability(project_id="apdl"),
            "apdl",
            "checkout",
            user_id="user-1",
            log_exposure=True,
            message_id=message_id,
        )


@pytest.mark.asyncio
async def test_flag_mutation_uses_approval_effect_config_write_authority(
    monkeypatch,
    capability_authority,
):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"created": True}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _path, *, json, params, headers):
            assert json == {"key": "checkout"}
            assert params == {"project_id": "apdl"}
            assert headers == {CAPABILITY_HEADER: f"apdlcap_{'F' * 43}"}
            return FakeResponse()

    monkeypatch.setattr(flags.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    capability = make_mutation_capability(project_id="apdl")

    await flags._post(
        capability,
        "apdl",
        "/v1/admin/flags",
        {"key": "checkout"},
        params={"project_id": "apdl"},
    )

    assert capability_authority[-1] == {
        "context": capability,
        "audiences": ("config",),
        "roles": ("config:write",),
        "request_method": "POST",
        "request_path": "/v1/admin/flags",
        "request_json": {"key": "checkout"},
        "idempotency_key": None,
    }
