import pytest

from app.safety.rollback import ExperimentRollbackMonitor, MetricSnapshot


@pytest.mark.asyncio
async def test_execute_rollback_is_disabled_for_oss_preview():
    monitor = ExperimentRollbackMonitor()
    result = await monitor.execute_rollback("apdl", "checkout-gate")

    assert result is False


@pytest.mark.asyncio
async def test_evaluate_never_turns_snapshot_statistics_into_rollback_readiness():
    decision = await ExperimentRollbackMonitor().evaluate(
        "apdl",
        "checkout-experiment",
        MetricSnapshot(error_rate=0.01, p95_latency_ms=200, primary_metric_value=0.2),
    )

    assert decision.should_rollback is False
    assert decision.current is None
    assert "deployment readiness" in decision.reasons[0]
