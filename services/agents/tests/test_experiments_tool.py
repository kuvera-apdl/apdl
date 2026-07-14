from datetime import datetime

import pytest

from app.tools import experiments


@pytest.mark.asyncio
async def test_get_experiment_results_uses_authoritative_query_contract(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "analysis_status": "insufficient_data",
                "reason": "underpowered_arms",
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, path, *, params):
            captured.update(path=path, params=params)
            return FakeResponse()

    monkeypatch.setattr(experiments.httpx, "AsyncClient", FakeClient)

    result = await experiments.get_experiment_results(
        experiment_id="exp checkout",
        project_id="apdl",
    )

    assert result["analysis_status"] == "insufficient_data"
    assert captured == {
        "path": "/v1/query/experiment/exp%20checkout",
        "params": {"project_id": "apdl"},
    }


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
        default_variant="control",
        primary_metric={"event": "purchase", "type": "conversion", "direction": "increase"},
        targeting={"conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}]},
    )

    assert result == {"created": True, "key": "exp_checkout"}
    assert captured["path"] == "/v1/admin/experiments"
    assert captured["params"] == {"project_id": "apdl"}
    payload = captured["json"]
    start_date = datetime.fromisoformat(payload.pop("start_date"))
    end_date = datetime.fromisoformat(payload.pop("end_date"))
    assert start_date.tzinfo is not None
    assert (end_date - start_date).days == 14
    assert payload == {
        "key": "exp_checkout",
        "flag_key": "exp_checkout",
        "status": "running",
        "description": "Checkout changes should improve conversion.",
        "variants": [
            {"key": "control", "weight": 1, "description": "Current checkout"},
            {"key": "treatment", "weight": 1, "description": "New checkout"},
        ],
        "default_variant": "control",
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
async def test_targeting_rejects_aliases_and_extra_fields(monkeypatch):
    _capture_post(monkeypatch)

    with pytest.raises(ValueError, match="requires exactly"):
        await experiments.create_experiment_config(
            project_id="apdl",
            experiment_id="exp_x",
            hypothesis="h",
            variants=[
                {"key": "control", "weight": 50},
                {"key": "treatment", "weight": 50},
            ],
            default_variant="control",
            primary_metric={"event": "purchase"},
            targeting={
                "conditions": [
                    {
                        "value": 1,
                        "operator": "equals",
                        "attribute": "session_count",
                        "description": "not canonical",
                    }
                ]
            },
        )

    with pytest.raises(ValueError, match="unsupported targeting operator"):
        await experiments.create_experiment_config(
            project_id="apdl",
            experiment_id="exp_x",
            hypothesis="h",
            variants=[
                {"key": "control", "weight": 50},
                {"key": "treatment", "weight": 50},
            ],
            default_variant="control",
            primary_metric={"event": "purchase"},
            targeting={
                "conditions": [
                    {"operator": "is_not_null", "attribute": "user_id"},
                ]
            },
        )


@pytest.mark.asyncio
async def test_targeting_presence_condition_uses_omitted_value(monkeypatch):
    captured = _capture_post(monkeypatch)

    await experiments.create_experiment_config(
        project_id="apdl",
        experiment_id="exp_x",
        hypothesis="h",
        variants=[{"key": "control", "weight": 50}, {"key": "treatment", "weight": 50}],
        default_variant="control",
        primary_metric={"event": "purchase"},
        targeting={
            "conditions": [
                {"operator": "exists", "attribute": "user_id"},
            ]
        },
    )

    assert captured["json"]["targeting_rules"] == [
        {
            "id": "targeting",
            "conditions": [
                {"attribute": "user_id", "operator": "exists"},
            ],
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        }
    ]


@pytest.mark.asyncio
async def test_empty_targeting_conditions_are_omitted(monkeypatch):
    captured = _capture_post(monkeypatch)

    await experiments.create_experiment_config(
        project_id="apdl",
        experiment_id="exp_y",
        hypothesis="h",
        variants=[{"key": "control", "weight": 1}, {"key": "treatment", "weight": 1}],
        default_variant="control",
        primary_metric={"event": "purchase"},
        targeting={"conditions": []},
    )

    assert "targeting_rules" not in captured["json"]


@pytest.mark.asyncio
async def test_running_experiment_requires_metric_and_bounded_duration(monkeypatch):
    _capture_post(monkeypatch)
    common = {
        "project_id": "apdl",
        "experiment_id": "exp_y",
        "hypothesis": "h",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
        "default_variant": "control",
    }

    with pytest.raises(ValueError, match="primary_metric.event"):
        await experiments.create_experiment_config(**common, primary_metric={})
    with pytest.raises(ValueError, match="estimated_duration_days"):
        await experiments.create_experiment_config(
            **common,
            primary_metric={"event": "purchase"},
            estimated_duration_days=0,
        )


def test_automatic_experiment_status_mutator_is_not_exposed():
    assert not hasattr(experiments, "update_experiment_status")
