"""Deterministic prompt/review rendering for verification contracts."""

from __future__ import annotations

import json

from app.verification.models import VerificationCoverage, VerificationPlan


def _json(value: VerificationPlan | VerificationCoverage) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def render_verification_plan(plan: VerificationPlan) -> str:
    return (
        "# GitHub CI verification plan\n\n"
        "Add the planned coverage without suppressing, skipping, or weakening "
        "existing checks. Existing protected workflow gates may only be preserved "
        "or strengthened. APDL does not execute authoritative verification and "
        "must not describe this plan as passed; only an exact GitHub result for "
        "the PR head can do that.\n\n"
        f"```json\n{_json(plan)}\n```"
    )


def render_verification_coverage(coverage: VerificationCoverage) -> str:
    return (
        "# Verification coverage inspection\n\n"
        "This inspection records whether coverage files are present in the diff. "
        "It is not a test result. GitHub has not reported, and APDL has not "
        "declared the change verified.\n\n"
        f"```json\n{_json(coverage)}\n```"
    )
