"""The experiment_design agent canonicalizes the LLM's flag_config before the
safety validator sees it — otherwise descriptive variant fields and rollout-less
rules halt otherwise-sound experiments at the variant_config check."""

from __future__ import annotations

from app.graphs.experiment_design import _canonicalize_flag_config
from app.safety.validator import ActionType, AgentAction, SafetyValidator


def _design() -> dict:
    return {
        "experiment_id": "exp_demo",
        "hypothesis": "A sufficiently long hypothesis to satisfy the guardrail check.",
        "description": "desc",
        "variants": [
            {"key": "control", "weight": 50, "description": "human-readable rationale"},
            {"key": "treatment", "weight": 50, "description": "human-readable rationale"},
        ],
        "primary_metric": {"event": "page", "type": "count", "direction": "increase"},
        "guardrail_metrics": [{"event": "$frontend_error", "threshold": "x", "direction": "decrease"}],
        "targeting": {"conditions": []},
        "flag_config": {
            "key": "demo-flag",
            "name": "Demo",
            "default_variant": "control",
            "variants": [
                {"key": "control", "weight": 1, "description": "drop me"},
                {"key": "treatment", "weight": 1, "description": "drop me"},
            ],
            "rules": [
                {
                    "variant": "control",
                    "conditions": [{"attribute": "platform", "operator": "contains", "value": "web"}],
                    "description": "non-canonical rule with no rollout",
                }
            ],
            "fallthrough": {"rollout": {"percentage": 100, "bucket_by": "user_id"}},
            "evaluation_mode": "client",
            "auto_disable": True,
        },
    }


def _validate(design: dict):
    return SafetyValidator().validate(
        AgentAction(type=ActionType.create_experiment, config=design, project_id="sanitize-test")
    )


def test_canonicalize_strips_variant_descriptions_and_noncanonical_rules() -> None:
    design = _design()
    _canonicalize_flag_config(design)

    assert design["flag_config"]["variants"] == [
        {"key": "control", "weight": 1},
        {"key": "treatment", "weight": 1},
    ]
    # The rollout-less rule is dropped; targeting still lives in `targeting`.
    assert design["flag_config"]["rules"] == []
    # Top-level variants keep their descriptions (config service allows them).
    assert design["variants"][0]["description"] == "human-readable rationale"


def test_sanitized_design_passes_safety_validator() -> None:
    # Before: the variant_config check fails on the description field.
    before = _validate(_design())
    assert not before.passed
    assert any(c["name"] == "variant_config" and not c["passed"] for c in before.checks)

    design = _design()
    _canonicalize_flag_config(design)
    after = _validate(design)
    assert after.passed, [c for c in after.checks if not c["passed"]]
