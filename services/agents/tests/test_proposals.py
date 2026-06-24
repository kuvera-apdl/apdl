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
