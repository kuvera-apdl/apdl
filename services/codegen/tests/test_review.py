"""Tests for the pre-push diff review (fail-open on infra, fail-closed on judgment)."""

import json

import pytest

from app.editor.review import _REVIEW_DIFF_CAP, review_change


def _make_complete(reply):
    calls = []

    async def complete(system: str, user: str):
        calls.append((system, user))
        return reply

    return complete, calls


@pytest.mark.asyncio
async def test_review_approves_and_feeds_spec_paths_and_diff():
    complete, calls = _make_complete(json.dumps({"approved": True, "problems": []}))

    verdict = await review_change(
        spec="Build the monitor.",
        diff_text="diff --git a/x b/x",
        changed_paths=["app/x.ts"],
        evidence_context='{"schema_version":"dependency_slice@1"}',
        complete=complete,
    )

    assert verdict.approved is True
    assert verdict.skipped is False
    _system, user = calls[0]
    assert "Build the monitor." in user
    assert "app/x.ts" in user
    assert "diff --git" in user
    assert "dependency_slice@1" in user


@pytest.mark.asyncio
async def test_review_rejects_with_problems_and_instructions():
    complete, _calls = _make_complete(
        "Here is my verdict:\n"
        + json.dumps(
            {
                "approved": False,
                "problems": ["nav link targets /dashboard/data-quality which does not exist"],
                "fix_instructions": "Create the page and wire the checks.",
            }
        )
    )

    verdict = await review_change(
        spec="s", diff_text="d", changed_paths=["p"], complete=complete
    )

    assert verdict.approved is False
    assert "does not exist" in verdict.problems[0]
    assert verdict.fix_instructions == "Create the page and wire the checks."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        None,  # model unavailable
        "I think this looks fine!",  # no JSON at all
        '{"approved": "yes"}',  # wrong type for the verdict field
    ],
)
async def test_review_skips_open_on_unusable_verdict(reply):
    complete, _calls = _make_complete(reply)

    verdict = await review_change(
        spec="s", diff_text="d", changed_paths=["p"], complete=complete
    )

    assert verdict.approved is True
    assert verdict.skipped is True


@pytest.mark.asyncio
async def test_review_caps_the_diff_but_marks_truncation():
    complete, calls = _make_complete(json.dumps({"approved": True}))

    await review_change(
        spec="s",
        diff_text="x" * (_REVIEW_DIFF_CAP + 100),
        changed_paths=["p"],
        complete=complete,
    )

    _system, user = calls[0]
    assert "diff truncated for review" in user
    assert len(user) < _REVIEW_DIFF_CAP + 2_000
