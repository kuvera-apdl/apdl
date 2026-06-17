import pytest

from app.tools import experiments


@pytest.mark.asyncio
async def test_create_experiment_config_uses_config_admin_schema(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"created": True, "key": "exp_checkout"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, path, *, json, params):
            captured["path"] = path
            captured["json"] = json
            captured["params"] = params
            return FakeResponse()

    monkeypatch.setattr(experiments.httpx, "AsyncClient", FakeClient)

    result = await experiments.create_experiment_config(
        project_id="apdl",
        experiment_id="exp_checkout",
        hypothesis="Checkout changes should improve conversion.",
        variants=[
            {"key": "control", "weight": 1, "description": "Current checkout"},
            {"key": "treatment", "weight": 1, "description": "New checkout"},
        ],
        primary_metric={"event": "purchase", "type": "conversion", "direction": "increase"},
        targeting={"conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}]},
    )

    assert result == {"created": True, "key": "exp_checkout"}
    assert captured["path"] == "/v1/admin/experiments"
    assert captured["params"] == {"project_id": "apdl"}
    assert captured["json"] == {
        "key": "exp_checkout",
        "flag_key": "exp_checkout",
        "status": "running",
        "description": "Checkout changes should improve conversion.",
        "variants": [
            {"key": "control", "weight": 1, "description": "Current checkout"},
            {"key": "treatment", "weight": 1, "description": "New checkout"},
        ],
        "traffic_percentage": 100.0,
        "primary_metric": {"event": "purchase", "type": "conversion", "direction": "increase"},
        "targeting_rules": [
            {
                "id": "targeting",
                "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
                "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
            },
        ],
    }
