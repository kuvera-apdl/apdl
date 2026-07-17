import pytest

from app.safety.rollback import ExperimentRollbackMonitor, RollbackUnavailableError


@pytest.mark.asyncio
async def test_execute_rollback_is_disabled_for_oss_preview():
    monitor = ExperimentRollbackMonitor()
    with pytest.raises(RollbackUnavailableError, match="rollback are unavailable"):
        await monitor.execute_rollback("apdl", "checkout-experiment")


@pytest.mark.asyncio
async def test_evaluate_cannot_be_mistaken_for_a_health_verdict():
    with pytest.raises(RollbackUnavailableError, match="guardrail"):
        await ExperimentRollbackMonitor().evaluate("apdl", "checkout-experiment")
