"""Regression tests for the project-scoped feature-proposal queue.

Real LLM proposals carry ``proposed_solution`` / ``implementation_spec`` /
``problem_statement`` — not ``spec`` / ``description``. The old ``_spec_of``
returned "" for them, so every proposal was silently dropped at enqueue and the
forked code-impl runs claimed nothing.
"""

from typing import Any

import pytest

from app.store.proposals import (
    _spec_of,
    claim_proposals,
    enqueue_proposals,
    get_proposal,
    list_recent_proposals,
    mark_failed,
    mark_implemented,
)


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Acquire:
    def __init__(self, conn: "_ProposalConn") -> None:
        self.conn = conn

    async def __aenter__(self) -> "_ProposalConn":
        return self.conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _ProposalConn:
    """Small SQL fake that preserves the table's composite tenant identity."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, query: str, *args: Any) -> str:
        if "INSERT INTO feature_proposals" in query:
            project_id, proposal_id, run_id, title, spec, priority = args
            key = (project_id, proposal_id)
            if key in self.rows:
                return "INSERT 0 0"
            self.rows[key] = {
                "project_id": project_id,
                "proposal_id": proposal_id,
                "run_id": run_id,
                "title": title,
                "spec": spec,
                "priority": priority,
                "status": "approved",
                "claim_run_id": None,
                "changeset_id": None,
                "error": None,
            }
            return "INSERT 0 1"

        if "SET status = 'implementing'" in query:
            project_id, proposal_ids, claim_run_id = args
            updated = 0
            for proposal_id in proposal_ids:
                row = self.rows.get((project_id, proposal_id))
                if row is not None and row["status"] == "approved":
                    row.update(
                        status="implementing",
                        claim_run_id=claim_run_id,
                        error=None,
                    )
                    updated += 1
            return f"UPDATE {updated}"

        project_id, proposal_id, value, claim_run_id = args
        row = self.rows.get((project_id, proposal_id))
        if (
            row is None
            or row["status"] != "implementing"
            or row["claim_run_id"] != claim_run_id
        ):
            return "UPDATE 0"
        if "SET status = 'implemented'" in query:
            row.update(
                status="implemented",
                changeset_id=value,
                claim_run_id=None,
            )
        elif "SET status = 'failed'" in query:
            row.update(status="failed", error=value, claim_run_id=None)
        else:
            raise AssertionError(query)
        return "UPDATE 1"

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        project_id = str(args[0])
        rows = [
            row
            for (row_project_id, _), row in self.rows.items()
            if row_project_id == project_id
        ]
        if "status = 'approved'" in query:
            rows = [row for row in rows if row["status"] == "approved"]
            if "proposal_id = $2" in query:
                rows = [row for row in rows if row["proposal_id"] == args[1]]
            else:
                rows = rows[: int(args[1])]
            fields = ("proposal_id", "title", "spec", "priority")
        else:
            rows = rows[: int(args[1])]
            fields = ("proposal_id", "title", "status")
        return [{field: row[field] for field in fields} for row in rows]

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "WHERE project_id = $1 AND proposal_id = $2" in query
        row = self.rows.get((str(args[0]), str(args[1])))
        return dict(row) if row is not None else None


class _Pool:
    def __init__(self) -> None:
        self.conn = _ProposalConn()

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


def _proposal(proposal_id: str, title: str) -> dict[str, str]:
    return {
        "proposal_id": proposal_id,
        "title": title,
        "proposed_solution": f"Implement {title} fully in the connected repository.",
    }


@pytest.mark.asyncio
async def test_same_proposal_id_is_independent_across_projects() -> None:
    pool = _Pool()

    assert await enqueue_proposals(
        pool, "run-a", "project_a", [_proposal("feat_shared", "Feature A")]
    ) == 1
    assert await enqueue_proposals(
        pool, "run-b", "project_b", [_proposal("feat_shared", "Feature B")]
    ) == 1
    assert await enqueue_proposals(
        pool, "run-a-retry", "project_a", [_proposal("feat_shared", "Retry")]
    ) == 0

    project_a = await get_proposal(pool, "project_a", "feat_shared")
    project_b = await get_proposal(pool, "project_b", "feat_shared")
    assert project_a is not None and project_a["title"] == "Feature A"
    assert project_b is not None and project_b["title"] == "Feature B"
    assert [row["title"] for row in await list_recent_proposals(pool, "project_a")] == [
        "Feature A"
    ]

    claimed_a = await claim_proposals(
        pool, "project_a", "implement-a", proposal_id="feat_shared"
    )
    assert [row["title"] for row in claimed_a] == ["Feature A"]
    assert pool.conn.rows[("project_b", "feat_shared")]["status"] == "approved"

    claimed_b = await claim_proposals(
        pool, "project_b", "implement-b", proposal_id="feat_shared"
    )
    assert [row["title"] for row in claimed_b] == ["Feature B"]

    await mark_implemented(
        pool, "project_a", "feat_shared", "changeset-a", "implement-a"
    )
    await mark_failed(
        pool, "project_b", "feat_shared", "rejected", "implement-b"
    )

    assert pool.conn.rows[("project_a", "feat_shared")]["status"] == "implemented"
    assert pool.conn.rows[("project_a", "feat_shared")]["changeset_id"] == "changeset-a"
    assert pool.conn.rows[("project_b", "feat_shared")]["status"] == "failed"
    assert pool.conn.rows[("project_b", "feat_shared")]["error"] == "rejected"


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
