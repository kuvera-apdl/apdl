"""Focused tests for the strict Phase-3 requirement ledger contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.requirements import (
    GitHubCheckExpectation,
    ImplementationEvidence,
    ImplementationEvidenceKind,
    ImplementationStatus,
    ObservableAssertionExpectation,
    Requirement,
    RequirementCompilationError,
    RequirementLedger,
    RequirementRisk,
    RequirementSourceKind,
    compile_requirement_ledger,
    render_requirement_ledger,
)


def _requirement(**overrides) -> Requirement:
    values = {
        "requirement_id": "REQ-001",
        "source_kind": RequirementSourceKind.acceptance_criterion,
        "original_source_text": "The toggle changes the theme.",
        "observable_behavior": "The toggle changes the theme.",
        "implementable_scope": "Wire the existing toggle to the theme state.",
        "expected_ci_evidence": [
            ObservableAssertionExpectation(
                evidence_id="CI-REQ-001-01",
                assertion="The toggle changes the theme.",
            )
        ],
        "risk": RequirementRisk.low,
    }
    values.update(overrides)
    return Requirement(**values)


def test_compiler_preserves_core_every_acceptance_criterion_and_constraint():
    spec = """## Goal
Add an accessible theme toggle.

## Acceptance criteria
1. The toggle is reachable by keyboard.
2. The selected theme persists after reload.
   This includes a fresh browser tab.

## Notes
Use the existing theme provider.
"""
    ledger = compile_requirement_ledger(
        title="Theme toggle",
        spec=spec,
        constraints=[
            "Do not add a second theme provider.",
            "All existing tests must pass.",
        ],
    )

    assert [item.requirement_id for item in ledger.requirements] == [
        "REQ-001",
        "REQ-002",
        "REQ-003",
        "REQ-004",
        "REQ-005",
    ]
    assert [item.source_kind for item in ledger.requirements] == [
        RequirementSourceKind.task_spec,
        RequirementSourceKind.acceptance_criterion,
        RequirementSourceKind.acceptance_criterion,
        RequirementSourceKind.constraint,
        RequirementSourceKind.constraint,
    ]
    sources = [item.original_source_text for item in ledger.requirements]
    assert "Add an accessible theme toggle." in sources[0]
    assert "Use the existing theme provider." in sources[0]
    assert sources[1] == "The toggle is reachable by keyboard."
    assert sources[2] == (
        "The selected theme persists after reload.\n"
        "This includes a fresh browser tab."
    )
    assert sources[3:] == [
        "Do not add a second theme provider.",
        "All existing tests must pass.",
    ]
    assert all(item.expected_ci_evidence for item in ledger.requirements)


def test_compilation_is_stable_for_identical_source():
    args = {
        "title": "Strict API",
        "spec": "## Acceptance criteria\n- Unknown request fields are rejected.",
        "constraints": ["Keep the response schema strict."],
        "risk": "medium",
        "verification_command": "pytest -q",
        "verification_cwd": "services/api",
    }

    first = compile_requirement_ledger(**args)
    second = compile_requirement_ledger(**args)

    assert first == second
    assert first.source_sha256 == second.source_sha256
    assert first.requirements[0].requirement_id == "REQ-001"
    mapping = first.requirements[0].expected_ci_evidence[0]
    assert mapping.kind == "repository_command"
    assert mapping.command == "pytest -q"
    assert mapping.cwd == "services/api"


def test_known_github_check_is_an_exact_expected_mapping():
    ledger = compile_requirement_ledger(
        title="API contract",
        spec="Reject unknown request fields.",
        github_check_name="api / pytest",
        verification_command="pytest -q",
    )

    mapping = ledger.requirements[0].expected_ci_evidence[0]
    assert isinstance(mapping, GitHubCheckExpectation)
    assert mapping.check_name == "api / pytest"
    assert mapping.evidence_id == "CI-REQ-001-01"


def test_explicit_blocker_and_descoping_require_reasons_and_remain_visible():
    ledger = compile_requirement_ledger(
        title="Integration",
        spec="Implement the in-repo API client.",
        constraints=[
            "Blocked: provision production credentials — requires platform admin access",
            "Out of scope: notify the legal team — requires a human owner",
        ],
    )

    blocked, descoped = ledger.requirements[1:]
    assert blocked.implementation_status is ImplementationStatus.blocked
    assert blocked.observable_behavior == "provision production credentials"
    assert blocked.decision_reason == "requires platform admin access"
    assert blocked.expected_ci_evidence == []
    assert descoped.implementation_status is ImplementationStatus.descoped
    assert descoped.observable_behavior == "notify the legal team"
    assert descoped.decision_reason == "requires a human owner"
    assert descoped.expected_ci_evidence == []

    with pytest.raises(RequirementCompilationError, match="needs a reason"):
        compile_requirement_ledger(
            title="Bad decision",
            spec="Blocked: provision production credentials",
        )


def test_schema_rejects_extra_fields_at_every_boundary():
    ledger = compile_requirement_ledger(title="T", spec="Do the thing.")
    payload = ledger.model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RequirementLedger.model_validate(payload)

    requirement_payload = ledger.requirements[0].model_dump(mode="json")
    requirement_payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Requirement.model_validate(requirement_payload)

    expectation_payload = requirement_payload["expected_ci_evidence"][0]
    expectation_payload["unexpected"] = True
    requirement_payload.pop("unexpected")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Requirement.model_validate(requirement_payload)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"expected_ci_evidence": []}, "expected CI evidence"),
        (
            {
                "implementation_status": ImplementationStatus.implemented,
                "implementation_evidence": [],
            },
            "changed file or symbol",
        ),
        (
            {
                "implementation_status": ImplementationStatus.blocked,
                "expected_ci_evidence": [],
            },
            "decision_reason",
        ),
    ],
)
def test_implementation_and_ci_mapping_invariants(overrides, message):
    with pytest.raises(ValidationError, match=message):
        _requirement(**overrides)


def test_confirmed_existing_and_implemented_evidence_are_distinct():
    existing = _requirement(
        implementation_status=ImplementationStatus.confirmed_existing,
        implementation_evidence=[
            ImplementationEvidence(
                kind=ImplementationEvidenceKind.existing,
                path="app/theme.ts",
                symbol="setTheme",
                description="Existing implementation already persists the selection.",
            )
        ],
    )
    implemented = _requirement(
        implementation_status=ImplementationStatus.implemented,
        implementation_evidence=[
            ImplementationEvidence(
                kind=ImplementationEvidenceKind.changed,
                path="app/toggle.tsx",
                symbol="ThemeToggle",
                description="The changed handler updates the theme provider.",
            )
        ],
    )

    assert existing.implementation_status is ImplementationStatus.confirmed_existing
    assert implemented.implementation_status is ImplementationStatus.implemented
    with pytest.raises(ValidationError, match="existing-behavior evidence"):
        _requirement(
            implementation_status=ImplementationStatus.confirmed_existing,
            implementation_evidence=implemented.implementation_evidence,
        )


def test_ledger_requires_canonical_contiguous_ids_and_namespaced_ci_ids():
    second = _requirement(
        requirement_id="REQ-002",
        expected_ci_evidence=[
            ObservableAssertionExpectation(
                evidence_id="CI-REQ-002-01",
                assertion="The toggle changes the theme.",
            )
        ],
    )
    with pytest.raises(ValidationError, match="contiguous"):
        RequirementLedger(
            title="T",
            source_sha256="a" * 64,
            requirements=[second],
        )

    with pytest.raises(ValidationError, match="namespaced"):
        _requirement(
            expected_ci_evidence=[
                ObservableAssertionExpectation(
                    evidence_id="CI-REQ-002-01",
                    assertion="The toggle changes the theme.",
                )
            ]
        )


def test_render_includes_complete_model_context_and_authority_boundary():
    ledger = compile_requirement_ledger(
        title="Theme toggle",
        spec="## Acceptance criteria\n- The selected theme persists after reload.",
        constraints=["Do not add a second theme provider."],
    )

    rendered = render_requirement_ledger(ledger)

    assert "requirement_ledger@1" in rendered
    assert "REQ-001" in rendered and "REQ-002" in rendered
    assert "The selected theme persists after reload." in rendered
    assert "Do not add a second theme provider." in rendered
    assert "Do not silently" in rendered
    assert "GitHub, not APDL" in rendered


def test_ready_for_pull_request_requires_every_active_requirement_to_be_resolved():
    ledger = compile_requirement_ledger(title="T", spec="Do the thing.")
    assert ledger.ready_for_pull_request() is False

    resolved = ledger.model_copy(
        update={
            "requirements": [
                ledger.requirements[0].model_copy(
                    update={
                        "implementation_status": ImplementationStatus.implemented,
                        "implementation_evidence": [
                            ImplementationEvidence(
                                kind=ImplementationEvidenceKind.changed,
                                path="app.py",
                                description="The requested behavior is wired.",
                            )
                        ],
                    }
                )
            ]
        }
    )
    assert resolved.ready_for_pull_request() is True


@pytest.mark.parametrize("field", ["title", "spec"])
def test_compiler_rejects_blank_required_source(field):
    values = {"title": "T", "spec": "Do it."}
    values[field] = "  "
    with pytest.raises(RequirementCompilationError):
        compile_requirement_ledger(**values)
