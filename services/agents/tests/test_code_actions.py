"""Safety validation for the code action types (open/merge pull request)."""

import pytest

from app.safety import validator
from app.safety.validator import ActionType, AgentAction, SafetyValidator


@pytest.fixture(autouse=True)
def clear_rate_limit_state():
    validator._action_timestamps.clear()


def _validate(action_type: ActionType, config: dict) -> dict:
    return (
        SafetyValidator()
        .validate(AgentAction(type=action_type, config=config, project_id="apdl"))
        .model_dump()
    )


def _check(result: dict, name: str) -> dict:
    return next(c for c in result["checks"] if c["name"] == name)


def test_open_pull_request_passes_and_is_low_risk():
    result = _validate(
        ActionType.open_pull_request,
        {"title": "Add dark mode", "spec": "Implement a dark-mode toggle across the app."},
    )
    assert result["passed"] is True
    assert result["risk_level"] == "low"


def test_open_pull_request_requires_a_real_spec():
    result = _validate(ActionType.open_pull_request, {"title": "x", "spec": "short"})
    assert result["passed"] is False
    assert "spec" in _check(result, "guardrails")["message"].lower()


def test_merge_requires_green_ci():
    result = _validate(
        ActionType.merge_pull_request,
        {"changeset_id": "cs_1", "ci_status": "failed", "diff_stat": {"files": 2}},
    )
    assert result["passed"] is False
    assert _check(result, "guardrails")["passed"] is False


def test_merge_passes_with_green_ci_and_is_high_risk():
    result = _validate(
        ActionType.merge_pull_request,
        {"changeset_id": "cs_1", "ci_status": "passed", "diff_stat": {"files": 3}},
    )
    assert result["passed"] is True
    assert result["risk_level"] == "high"


def test_merge_blast_radius_caps_huge_diffs():
    result = _validate(
        ActionType.merge_pull_request,
        {"changeset_id": "cs_1", "ci_status": "passed", "diff_stat": {"files": 80}},
    )
    assert result["passed"] is False
    assert _check(result, "blast_radius")["passed"] is False
