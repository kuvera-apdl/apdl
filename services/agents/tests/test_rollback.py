import pytest

from app.safety.rollback import ExperimentRollbackMonitor


@pytest.mark.asyncio
async def test_execute_rollback_is_disabled_for_oss_preview():
    monitor = ExperimentRollbackMonitor()
    result = await monitor.execute_rollback("apdl", "checkout-gate")

    assert result is False
