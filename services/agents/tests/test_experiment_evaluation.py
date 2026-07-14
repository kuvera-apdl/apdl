"""The OSS preview's autonomous experiment evaluator is fail-closed."""

import pytest

from app.framework.registry import registered_agents
from app.graphs.experiment_evaluation import ExperimentEvaluationAgent


def test_experiment_evaluation_is_registered_but_disabled():
    registered = registered_agents()

    assert registered["experiment_evaluation"] is ExperimentEvaluationAgent
    assert ExperimentEvaluationAgent.enabled is False


@pytest.mark.asyncio
async def test_disabled_evaluator_never_builds_prompt_or_attempts_mutation():
    agent = ExperimentEvaluationAgent()
    context = object()

    working = await agent.gather(context, {}, {})
    assert working == {"disabled": True}
    assert agent.build_prompt(context, {}, working) is None
    assert await agent.act(context, {}, working, []) == {
        "disabled": True,
        "evaluated": 0,
        "mutations_attempted": 0,
    }
