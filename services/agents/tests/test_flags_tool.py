from typing import Any

import pytest

from app.tools import flags


@pytest.mark.asyncio
async def test_create_flag_derives_enabled_from_active_state(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_post(
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
        project_id: str,
        path: str,
        payload: dict[str, Any],
        params: dict[str, Any] | None = None,
    ):
        captured["payload"] = payload
        return {"created": True, "flag": payload}

    monkeypatch.setattr(flags, "_post", fake_post)

    await flags.create_flag(
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
        project_id: str,
        path: str,
        payload: dict[str, Any],
        params: dict[str, Any] | None = None,
    ):
        captured["payload"] = payload
        return {"created": True, "flag": payload}

    monkeypatch.setattr(flags, "_post", fake_post)

    await flags.create_flag(
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
        project_id: str,
        path: str,
        payload: dict[str, Any],
        params: dict[str, Any] | None = None,
    ):
        captured["payload"] = payload
        return {"created": True, "flag": payload}

    monkeypatch.setattr(flags, "_post", fake_post)

    await flags.create_flag(
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

    async def fake_post(project_id, path, payload, params=None):
        captured.update(
            project_id=project_id,
            path=path,
            payload=payload,
            params=params,
        )
        return {"updated": True}

    monkeypatch.setattr(flags, "_post", fake_post)

    await flags.transition_flag(
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

    async def fake_post(project_id, path, payload, params=None):
        captured.update(path=path, payload=payload, params=params)
        return {"disabled": True}

    monkeypatch.setattr(flags, "_post", fake_post)

    await flags.disable_flag(
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
