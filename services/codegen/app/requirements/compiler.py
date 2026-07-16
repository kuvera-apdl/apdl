"""Deterministic source task to :class:`RequirementLedger` compilation.

This compiler intentionally does not guess repository structure.  It preserves
the task's implementable core, every explicit acceptance criterion, and every
constraint as separate stable requirements.  Later repository-aware stages may
enrich likely targets and exact contract/CI evidence without changing IDs or
silently deleting requested behavior.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.requirements.models import (
    GitHubCheckExpectation,
    ImplementationStatus,
    ObservableAssertionExpectation,
    RepositoryCommandExpectation,
    Requirement,
    RequirementLedger,
    RequirementRisk,
    RequirementSourceKind,
)

_AC_HEADING = re.compile(
    r"^\s*(?P<hashes>#{1,6}\s*)?acceptance\s+criteri(?:on|a)\s*:?\s*$",
    re.IGNORECASE,
)
_MARKDOWN_HEADING = re.compile(r"^\s*(?P<hashes>#{1,6})\s+\S")
_LIST_ITEM = re.compile(
    r"^\s*(?:[-+*]|[0-9]+[.)])\s+(?:\[[ xX]\]\s*)?(?P<text>\S.*)$"
)
_DECISION = re.compile(
    r"^\s*(?P<kind>out\s+of\s+scope|descoped?|blocked)\s*:\s*(?P<body>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_REASON_SEPARATOR = re.compile(r"\s+(?:—|--|because\b|reason:)\s*", re.IGNORECASE)


class RequirementCompilationError(ValueError):
    """Raised when source requirements cannot become an honest strict ledger."""


@dataclass(frozen=True)
class _SourceItem:
    kind: RequirementSourceKind
    text: str


def _parse_list_items(lines: list[str]) -> list[str]:
    """Extract ordered criteria while retaining continuation text."""
    items: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            value = "\n".join(part.rstrip() for part in current).strip()
            if value:
                items.append(value)
            current.clear()

    saw_marker = False
    for line in lines:
        marker = _LIST_ITEM.match(line)
        if marker:
            saw_marker = True
            flush()
            current.append(marker.group("text").strip())
        elif saw_marker:
            if line.strip():
                current.append(line.strip())
            else:
                flush()
    flush()
    if saw_marker:
        return items

    # A prose acceptance section is still explicit. Preserve each paragraph as
    # one criterion rather than silently dropping the section.
    paragraphs: list[str] = []
    paragraph: list[str] = []
    for line in lines:
        if line.strip():
            paragraph.append(line.strip())
        elif paragraph:
            paragraphs.append("\n".join(paragraph))
            paragraph = []
    if paragraph:
        paragraphs.append("\n".join(paragraph))
    return paragraphs


def _split_spec(spec: str) -> tuple[str, list[str]]:
    """Return the non-acceptance core and all explicit acceptance criteria."""
    lines = spec.splitlines()
    core: list[str] = []
    criteria: list[str] = []
    index = 0
    while index < len(lines):
        heading = _AC_HEADING.match(lines[index])
        if heading is None:
            core.append(lines[index])
            index += 1
            continue

        hashes = (heading.group("hashes") or "").strip()
        level = len(hashes)
        index += 1
        section: list[str] = []
        while index < len(lines):
            next_heading = _MARKDOWN_HEADING.match(lines[index])
            if next_heading is not None:
                next_level = len(next_heading.group("hashes"))
                if level == 0 or next_level <= level:
                    break
            section.append(lines[index])
            index += 1
        extracted = _parse_list_items(section)
        if not extracted:
            raise RequirementCompilationError(
                "An Acceptance Criteria section exists but contains no criteria."
            )
        criteria.extend(extracted)

    return "\n".join(core).strip(), criteria


def _decision(text: str) -> tuple[ImplementationStatus, str, str | None]:
    """Recognize only explicit blocked/descoped statements with an explicit reason."""
    match = _DECISION.match(text)
    if match is None:
        return ImplementationStatus.planned, text, None

    body = match.group("body").strip()
    pieces = _REASON_SEPARATOR.split(body, maxsplit=1)
    if len(pieces) != 2 or not pieces[0].strip() or not pieces[1].strip():
        raise RequirementCompilationError(
            f"Explicit {match.group('kind').lower()} requirement needs a reason: {text}"
        )
    status = (
        ImplementationStatus.blocked
        if match.group("kind").lower() == "blocked"
        else ImplementationStatus.descoped
    )
    return status, pieces[0].strip(), pieces[1].strip()


def _source_hash(*, title: str, spec: str, constraints: Sequence[str]) -> str:
    payload = json.dumps(
        {"title": title, "spec": spec, "constraints": list(constraints)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compile_requirement_ledger(
    *,
    title: str,
    spec: str,
    constraints: Sequence[str] = (),
    risk: RequirementRisk | str = RequirementRisk.low,
    github_check_name: str | None = None,
    verification_command: str | None = None,
    verification_cwd: str = ".",
) -> RequirementLedger:
    """Compile a stable ledger without inventing repository-specific facts.

    A known exact GitHub check takes precedence as the strongest mapping.  A
    known repository command is otherwise recorded.  With neither, the ledger
    records an observable assertion; that is an expectation, never a claim that
    CI exists or has passed.
    """
    title = title.strip()
    spec = spec.strip()
    if not title:
        raise RequirementCompilationError("Requirement ledger title cannot be blank.")
    if not spec:
        raise RequirementCompilationError("Requirement ledger spec cannot be blank.")
    if github_check_name is not None and not github_check_name.strip():
        raise RequirementCompilationError("github_check_name cannot be blank.")
    if verification_command is not None and not verification_command.strip():
        raise RequirementCompilationError("verification_command cannot be blank.")
    if not verification_cwd.strip():
        raise RequirementCompilationError("verification_cwd cannot be blank.")
    try:
        risk_value = risk if isinstance(risk, RequirementRisk) else RequirementRisk(risk)
    except ValueError as exc:
        raise RequirementCompilationError(f"Unknown requirement risk: {risk}") from exc

    normalized_constraints = [constraint.strip() for constraint in constraints]
    if any(not constraint for constraint in normalized_constraints):
        raise RequirementCompilationError("Constraints cannot be blank.")

    core, criteria = _split_spec(spec)
    sources: list[_SourceItem] = []
    if core:
        sources.append(_SourceItem(RequirementSourceKind.task_spec, core))
    sources.extend(
        _SourceItem(RequirementSourceKind.acceptance_criterion, criterion)
        for criterion in criteria
    )
    sources.extend(
        _SourceItem(RequirementSourceKind.constraint, constraint)
        for constraint in normalized_constraints
    )
    if not sources:
        raise RequirementCompilationError("The task contains no requirements.")

    requirements: list[Requirement] = []
    for index, source in enumerate(sources, start=1):
        requirement_id = f"REQ-{index:03d}"
        status, behavior, reason = _decision(source.text)
        evidence = []
        if status is ImplementationStatus.planned:
            evidence_id = f"CI-{requirement_id}-01"
            if github_check_name:
                evidence = [
                    GitHubCheckExpectation(
                        evidence_id=evidence_id,
                        check_name=github_check_name.strip(),
                        assertion=behavior,
                    )
                ]
            elif verification_command:
                evidence = [
                    RepositoryCommandExpectation(
                        evidence_id=evidence_id,
                        command=verification_command.strip(),
                        cwd=verification_cwd.strip(),
                        assertion=behavior,
                    )
                ]
            else:
                evidence = [
                    ObservableAssertionExpectation(
                        evidence_id=evidence_id,
                        assertion=behavior,
                    )
                ]

        requirements.append(
            Requirement(
                requirement_id=requirement_id,
                source_kind=source.kind,
                original_source_text=source.text,
                observable_behavior=behavior,
                implementable_scope=(
                    behavior
                    if status is ImplementationStatus.planned
                    else "No repository-implementable scope."
                ),
                expected_ci_evidence=evidence,
                risk=risk_value,
                implementation_status=status,
                decision_reason=reason,
            )
        )

    return RequirementLedger(
        title=title,
        source_sha256=_source_hash(
            title=title, spec=spec, constraints=normalized_constraints
        ),
        requirements=requirements,
    )
