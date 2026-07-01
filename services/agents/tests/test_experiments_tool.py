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


def _capture_post(monkeypatch) -> dict:
    """Patch httpx.AsyncClient so create_experiment_config's POST is captured."""
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"created": True}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, path, *, json, params):
            captured.update(path=path, json=json, params=params)
            return _Resp()

    monkeypatch.setattr(experiments.httpx, "AsyncClient", _Client)
    return captured


@pytest.mark.asyncio
async def test_targeting_conditions_are_canonicalized(monkeypatch):
    """Regression for the config 422: loose LLM conditions (extra `description`,
    alias operator `is_not_null`) are projected onto the strict GateCondition
    shape, and unmappable operators are dropped."""
    captured = _capture_post(monkeypatch)

    await experiments.create_experiment_config(
        project_id="apdl",
        experiment_id="exp_x",
        hypothesis="h",
        variants=[{"key": "control", "weight": 50}, {"key": "treatment", "weight": 50}],
        primary_metric={"event": "purchase"},
        targeting={
            "conditions": [
                {"value": 1, "operator": "equals", "attribute": "session_count", "description": "drop"},
                {"operator": "is_not_null", "attribute": "user_id", "description": "drop"},
                {"operator": "between", "attribute": "age"},  # unmappable -> dropped
            ]
        },
    )

    assert captured["json"]["targeting_rules"] == [
        {
            "id": "targeting",
            "conditions": [
                {"attribute": "session_count", "operator": "equals", "value": 1},
                {"attribute": "user_id", "operator": "exists"},
            ],
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        }
    ]


@pytest.mark.asyncio
async def test_targeting_with_no_canonical_conditions_is_omitted(monkeypatch):
    captured = _capture_post(monkeypatch)

    await experiments.create_experiment_config(
        project_id="apdl",
        experiment_id="exp_y",
        hypothesis="h",
        variants=[{"key": "control", "weight": 1}, {"key": "treatment", "weight": 1}],
        primary_metric={"event": "purchase"},
        targeting={"conditions": [{"operator": "between", "attribute": "age"}]},
    )

    assert "targeting_rules" not in captured["json"]
