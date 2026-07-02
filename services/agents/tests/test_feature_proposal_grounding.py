"""Tests for the feature-proposal agent's repo + dedup grounding.

The proposal prompt used to fill its capabilities slot with a literal
placeholder string and knew nothing about prior proposals — every run
re-proposed the same themes as org-level briefs the coding agent could not
implement. These tests pin the two grounding channels: the rendered repo
context and the "already proposed or in flight" list.
"""

from app.graphs.feature_proposal import (
    FeatureProposalAgent,
    _render_existing_work,
    _render_repo_capabilities,
)


def test_render_repo_capabilities_full_document():
    text = _render_repo_capabilities(
        {
            "repo": "acme/widgets",
            "branch": "main",
            "framework": "Next.js (App Router)",
            "has_test_script": False,
            "scripts": {"build": "next build", "lint": "eslint"},
            "readme_excerpt": "# Keelstone\nDemo fintech site.",
            "paths": ["app/page.tsx", "lib/utils.ts"],
            "paths_truncated": True,
        }
    )

    assert "Repository: acme/widgets (branch main)" in text
    assert "Stack: Next.js (App Router)" in text
    assert "Test script present: no" in text
    assert "package.json scripts: build, lint" in text
    assert "Demo fintech site." in text
    assert "app/page.tsx" in text
    assert "(file list truncated)" in text


def test_render_repo_capabilities_degrades_explicitly_when_empty():
    text = _render_repo_capabilities({})
    assert "unavailable" in text


def test_render_existing_work_merges_and_dedupes_by_title():
    text = _render_existing_work(
        proposals=[
            {"title": "Bot filter", "status": "implemented"},
            {"title": "Aha moment framework", "status": "approved"},
        ],
        changesets=[
            {
                "task": {"title": "Bot filter"},  # same idea → one line only
                "status": "pr_open",
                "pr_url": "https://github.com/acme/widgets/pull/10",
            },
            {"task": {"title": "Health monitor"}, "status": "merged", "pr_url": None},
        ],
    )

    assert text.count("Bot filter") == 1
    assert "- Aha moment framework (proposal, approved)" in text
    assert "- Health monitor (changeset, merged)" in text


def test_render_existing_work_empty():
    assert _render_existing_work([], []) == "(none)"


def test_build_prompt_carries_repo_context_and_existing_work():
    agent = FeatureProposalAgent()
    working = {
        "experiment_results": [],
        "context": "",
        "repo_context": {
            "repo": "acme/widgets",
            "branch": "main",
            "framework": "Next.js (App Router)",
            "paths": ["app/page.tsx"],
        },
        "recent_proposals": [{"title": "Bot filter", "status": "implemented"}],
        "changesets": [],
    }

    prompt = agent.build_prompt(None, {"insights": []}, working)

    assert "Next.js (App Router)" in prompt
    assert "app/page.tsx" in prompt
    assert "- Bot filter (proposal, implemented)" in prompt
    # The old placeholder must never reach the model again.
    assert "(determined from project configuration)" not in prompt
