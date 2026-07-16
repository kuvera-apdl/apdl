"""Safety validation for the PR-creation action."""

from app.safety.validator import ActionType, AgentAction, SafetyValidator


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
