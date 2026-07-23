"""Spec → engineering-brief compilation (the pre-edit auxiliary LLM pass).

Approved feature proposals arrive written at product altitude: they can demand
organizational actions ("stakeholder sign-off", "alert the data engineering
team") and infrastructure the connected repository does not have (ETL pipelines,
Slack webhooks). Handing that raw text to the editing agent forces it to guess a
repo-shaped interpretation mid-edit — the observed failure modes are fabricated
in-memory "pipelines", and near-empty diffs when the agent reads the unmet
dependencies as a reason to descope to nothing.

This pass does the interpretation *before* the edit, with the actual clone in
hand: it translates the spec into a work order grounded in this repository —
concrete files to touch, explicit descoping decisions for anything that cannot
be code here, and acceptance criteria a reviewer can check in the repo. The
brief replaces the spec in the agent's message; the original spec remains the
contract the post-edit review judges against.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.editor.llm import CompleteFn
from app.inspection.repository import RepositoryInspector
from app.profiling import RepoProfile, profile_repository, render_profile

logger = logging.getLogger(__name__)

#: Path cap for the repo digest. Enough to show a real app's full shape; a
#: monorepo overflows and the digest says so rather than silently truncating.
_DIGEST_MAX_PATHS = 400
#: Directory names that never help the brief (dependencies, build output, VCS).
_DIGEST_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        "build",
        ".next",
        ".venv",
        "venv",
        "__pycache__",
        "vendor",
        "target",
        ".turbo",
        "coverage",
    }
)
#: A brief shorter than this cannot be a real work order; fall back to the spec.
_MIN_BRIEF_CHARS = 200

BRIEF_SYSTEM = """\
You compile approved product feature proposals into precise engineering briefs
for an automated coding agent. The agent can ONLY edit files in the one
repository described below — it cannot contact people, configure external
services, or touch other systems. Your brief is the agent's entire understanding
of the task.

Write the brief as a work order with exactly these sections:

## Goal
One paragraph: the user-visible outcome this change delivers in THIS repository.

## Scope decisions
The proposal may ask for things that cannot be code in this repository
(organizational actions, external infrastructure, human sign-off). List each
such item and rule on it explicitly: either translate it into the closest
in-repo equivalent, or descope it in one line ("out of scope: X — requires Y").
NEVER let an unimplementable item silently shrink the implementable core; the
agent must still build everything that CAN be built here.

## Implementation plan
Concrete and repo-grounded: which existing files to modify, which new files to
create (with paths that follow the repo's conventions), and how each piece is
wired into a reachable path (route, page, layout, registration). Name real
files from the repository digest — never invent paths for frameworks the repo
does not use.

## Acceptance criteria
A numbered checklist a reviewer can verify by reading the diff and running the
repo's verification command. Each criterion must be observable in this
repository (a route that renders, an event that fires, a function with given
behavior) — never an organizational outcome.

Rules:
- Preserve the proposal's full implementable intent. The brief may narrow HOW,
  never quietly narrow WHAT.
- Stay within the repository's existing stack and dependencies.
- Plain markdown only. No preamble before "## Goal", nothing after the criteria.
"""


def build_repo_digest(repo_dir: Path, profile: RepoProfile | None = None) -> str:
    """Canonical repository profile plus a bounded README excerpt."""
    contents = RepositoryInspector(repo_dir).text_view()
    sections = [
        "### Canonical repository profile\n"
        + render_profile(profile or profile_repository(repo_dir))
    ]

    for readme_name in ("README.md", "README.rst", "README"):
        inspected = contents.inspect(readme_name)
        if inspected is not None:
            head = inspected.text[:2000]
            sections.append(f"### README (head)\n{head}")
            break

    return "\n\n".join(sections)


def build_brief_user(
    *, title: str, spec: str, repo_digest: str, verification_context: str
) -> str:
    """The exact user message the brief pass sends.

    Shared with the editor's prompt transcript (``EditResult.prompts``) so what
    the admin console shows is byte-for-byte what the model received.
    """
    return (
        f"# Approved proposal\n\n## Title\n{title.strip()}\n\n"
        f"## Spec\n{spec.strip()}\n\n"
        f"# Repository digest\n\n{repo_digest.strip()}\n\n"
        f"# Repository verification\n\n{verification_context.strip()}"
    )


async def compile_brief(
    *,
    title: str,
    spec: str,
    repo_digest: str,
    verification_context: str,
    complete: CompleteFn,
) -> str | None:
    """Compile the spec into a repo-grounded brief; ``None`` means "use the spec".

    Fail-open by design: an unavailable or degenerate compilation must never
    block the changeset — the raw spec is what would have run anyway.
    """
    user = build_brief_user(
        title=title,
        spec=spec,
        repo_digest=repo_digest,
        verification_context=verification_context,
    )
    brief = await complete(BRIEF_SYSTEM, user)
    if (
        brief is None
        or len(brief) < _MIN_BRIEF_CHARS
        or not brief.startswith("## Goal\n")
    ):
        logger.warning(
            "Brief compilation produced no usable brief for %r; "
            "falling back to the raw spec.",
            title,
        )
        return None
    return brief
