"""Deterministic model context rendering for a requirement ledger."""

from __future__ import annotations

import json

from app.requirements.models import RequirementLedger


def render_requirement_ledger(ledger: RequirementLedger) -> str:
    """Render complete canonical JSON plus non-negotiable editing instructions."""
    payload = json.dumps(
        ledger.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return (
        "# Canonical requirement ledger\n\n"
        "Treat every requirement ID below as an independent contract. Preserve "
        "the original source text and observable behavior. Do not silently "
        "weaken, omit, merge, or renumber requirements. Blocked and descoped "
        "items are decisions to report, not work to fabricate. For every active "
        "requirement, implement or confirm the behavior and preserve its expected "
        "GitHub CI evidence mapping. GitHub, not APDL, supplies the eventual "
        "verification result.\n\n"
        f"```json\n{payload}\n```"
    )
