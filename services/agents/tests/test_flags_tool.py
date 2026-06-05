from typing import Any

import pytest

from app.tools import flags


@pytest.mark.asyncio
async def test_create_flag_derives_enabled_from_active_state(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_post(path: str, payload: dict[str, Any], params: dict[str, Any] | None = None):
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
    assert captured["params"] == {"project_id": "apdl"}
    assert captured["payload"]["state"] == "active"
    assert captured["payload"]["enabled"] is True


@pytest.mark.asyncio
async def test_create_flag_derives_draft_state_from_disabled_flag(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_post(path: str, payload: dict[str, Any], params: dict[str, Any] | None = None):
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
