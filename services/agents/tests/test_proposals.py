"""Regression tests for the feature-proposal queue spec extraction (Bug C).

Real LLM proposals carry ``proposed_solution`` / ``implementation_spec`` /
``problem_statement`` — not ``spec`` / ``description``. The old ``_spec_of``
returned "" for them, so every proposal was silently dropped at enqueue and the
forked code-impl runs claimed nothing.
"""

from app.store.proposals import _spec_of


def test_spec_of_uses_real_proposal_fields():
    proposal = {
        "proposal_id": "feat_x",
        "title": "X",
        "proposed_solution": "Build the X toggle.",
        "implementation_spec": {"files": ["a.py"], "steps": ["do it"]},
    }
    spec = _spec_of(proposal)
    assert "Build the X toggle." in spec  # human-readable prose
    assert "a.py" in spec  # serialized structured implementation detail


def test_spec_of_supports_legacy_spec_field():
    assert _spec_of({"spec": "legacy spec text"}) == "legacy spec text"


def test_spec_of_falls_back_to_problem_statement():
    assert _spec_of({"problem_statement": "the problem"}) == "the problem"


def test_spec_of_empty_when_no_usable_fields():
    assert _spec_of({"title": "only a title"}) == ""


def test_spec_of_keeps_proposal_with_only_implementation_spec():
    spec = _spec_of(
        {
            "implementation_spec": {
                "components_affected": ["app/page.tsx"],
                "technical_considerations": ["preserve the current route contract"],
            }
        }
    )

    assert spec.startswith("## Implementation notes\n")
    assert "app/page.tsx" in spec
    assert "preserve the current route contract" in spec


def test_spec_of_keeps_proposal_with_only_success_criteria():
    spec = _spec_of(
        {
            "success_criteria": [
                {
                    "metric": "checkout completion",
                    "target": "+5%",
                    "timeframe": "14 days",
                }
            ]
        }
    )

    assert spec == "## Acceptance criteria\n- checkout completion — +5% (within 14 days)"


def test_spec_of_renders_structured_fields_as_markdown_not_json():
    proposal = {
        "problem_statement": "53% of sessions never scroll.",
        "proposed_solution": "Move the lead form above the fold.",
        "implementation_spec": {
            "components_affected": ["app/page.tsx"],
            "technical_considerations": ["keep the hero image"],
            "dependencies": ["hero component must accept a slot"],
            "estimated_effort": "small",
        },
        "success_criteria": [
            {"metric": "form_submit rate", "target": "+10%", "timeframe": "14 days"}
        ],
    }

    spec = _spec_of(proposal)

    assert "## Problem\n53% of sessions never scroll." in spec
    assert "## What to build\nMove the lead form above the fold." in spec
    assert "**Components affected:**\n- app/page.tsx" in spec
    assert "**Technical considerations:**\n- keep the hero image" in spec
    assert "**In-repo prerequisites:**\n- hero component must accept a slot" in spec
    assert "**Estimated effort:** small" in spec
    assert "## Acceptance criteria\n- form_submit rate — +10% (within 14 days)" in spec
    # The old raw-JSON dump must be gone from the rendered work order.
    assert '{"components_affected"' not in spec


def test_spec_of_serializes_unknown_impl_fields_so_nothing_is_dropped():
    spec = _spec_of(
        {
            "proposed_solution": "Do it.",
            "implementation_spec": {"files": ["a.py"], "steps": ["do it"]},
        }
    )
    assert "a.py" in spec


def test_spec_of_accepts_string_success_criteria():
    spec = _spec_of(
        {"proposed_solution": "Do it.", "success_criteria": ["route renders"]}
    )
    assert "## Acceptance criteria\n- route renders" in spec
