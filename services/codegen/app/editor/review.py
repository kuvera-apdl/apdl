"""Post-edit quality review (the pre-push auxiliary LLM pass).

The verification command proves the change *builds*; it says nothing about
whether the change *delivers*. The observed failure this closes: a spec asking
for a monitoring layer shipped as a two-line diff adding a nav link to a page
that was never created — green build, empty feature. This pass judges the diff
against the original spec before the branch is pushed; a rejection feeds one
retry with the reviewer's instructions, then fails the changeset.

Fail-open on infrastructure, fail-closed on judgment: an unavailable model or an
unparseable verdict skips the gate (an auxiliary pass must not sink good
changes), but a parsed rejection blocks the push.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.editor.llm import CompleteFn

logger = logging.getLogger(__name__)

#: Diff budget for the review prompt. Reviews judge coverage and reachability,
#: not line-by-line correctness, so a bounded window is enough; the marker tells
#: the reviewer the diff continues.
_REVIEW_DIFF_CAP = 60_000

REVIEW_SYSTEM = """\
You review a code change produced by an automated coding agent against the
approved task it was meant to implement. You see the task spec and the full
diff. Decide whether the diff plausibly DELIVERS the task, not merely compiles.

Judge only what a code change in this one repository could deliver. Items in
the spec that require organizational action or external infrastructure
(other teams, sign-offs, third-party services the repo is not wired to) are out
of scope — never reject for those. Reject when:

1. The diff implements little or none of the spec's repo-implementable core —
   a token gesture (a link, a stub, a comment) standing in for the feature.
2. Something the diff adds is unreachable: a component never rendered on any
   route, a link or fetch pointing at a route/file that does not exist in the
   repo or the diff, an export nothing imports.
3. The diff fabricates a capability instead of wiring a real one — e.g. code
   that stores to a throwaway in-memory structure while claiming to integrate
   with a system the repo actually exposes.

Do NOT reject for style, for incomplete test coverage, or for descoped items
the diff explicitly marks out of scope with a reason.

Answer with ONLY a JSON object, no other text:
{
  "approved": true | false,
  "problems": ["<each concrete defect, with the file/path involved>"],
  "fix_instructions": "<imperative instructions the coding agent can act on to fix the problems; empty when approved>"
}
"""


@dataclass(frozen=True)
class ReviewVerdict:
    """Outcome of one diff review."""

    approved: bool
    problems: tuple[str, ...] = ()
    fix_instructions: str = ""
    #: True when the gate did not actually run (model unavailable / unparseable
    #: verdict) and ``approved`` is the fail-open default, not a judgment.
    skipped: bool = False


_SKIPPED = ReviewVerdict(approved=True, skipped=True)


def _parse_verdict(text: str) -> ReviewVerdict | None:
    """Extract the JSON verdict from a completion (``None`` when unparseable)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("approved"), bool):
        return None
    problems = data.get("problems")
    if not isinstance(problems, list):
        problems = []
    return ReviewVerdict(
        approved=data["approved"],
        problems=tuple(str(p) for p in problems),
        fix_instructions=str(data.get("fix_instructions") or ""),
    )


async def review_change(
    *,
    spec: str,
    diff_text: str,
    changed_paths: list[str],
    complete: CompleteFn,
) -> ReviewVerdict:
    """Judge ``diff_text`` against ``spec``; skipped (approved) on infra failure."""
    diff = diff_text[:_REVIEW_DIFF_CAP]
    if len(diff_text) > _REVIEW_DIFF_CAP:
        diff += "\n[…diff truncated for review…]"
    user = (
        f"# Task spec\n\n{spec.strip()}\n\n"
        f"# Changed files\n\n{chr(10).join(changed_paths)}\n\n"
        f"# Diff\n\n```diff\n{diff}\n```"
    )
    text = await complete(REVIEW_SYSTEM, user)
    if text is None:
        return _SKIPPED
    verdict = _parse_verdict(text)
    if verdict is None:
        logger.warning("Diff review returned an unparseable verdict; skipping the gate.")
        return _SKIPPED
    return verdict
