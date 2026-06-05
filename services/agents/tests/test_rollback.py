import pytest

from app.safety import rollback
from app.safety.rollback import ExperimentRollbackMonitor


@pytest.mark.asyncio
async def test_execute_rollback_uses_canonical_disable_endpoint(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, path, *, params, json):
            captured["path"] = path
            captured["params"] = params
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(rollback.httpx, "AsyncClient", FakeClient)

    monitor = ExperimentRollbackMonitor()
    result = await monitor.execute_rollback("apdl", "checkout-gate")

    assert result is True
    assert captured["path"] == "/v1/admin/flags/checkout-gate/disable"
    assert captured["params"] == {"project_id": "apdl"}
    assert captured["json"] == {
        "reason": "experiment_rollback",
        "source": "system",
        "evidence": {
            "rollback_monitor": "experiment",
        },
    }
