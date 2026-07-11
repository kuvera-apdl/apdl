"""Bind exact contract and implementation evidence to stable requirements."""

from __future__ import annotations

import json
import re

from app.contracts.models import ContractBundle
from app.requirements.models import (
    ImplementationEvidence,
    ImplementationEvidenceKind,
    ImplementationStatus,
    RequirementLedger,
)

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


def _validated_copy(ledger: RequirementLedger, requirements: list[dict]) -> RequirementLedger:
    payload = ledger.model_dump(mode="json")
    payload["requirements"] = requirements
    return RequirementLedger.model_validate_json(json.dumps(payload))


def bind_contract_evidence(
    ledger: RequirementLedger,
    bundle: ContractBundle,
) -> RequirementLedger:
    """Attach ready exact contracts only to their named requirement IDs."""
    by_requirement: dict[str, set[str]] = {}
    known = {item.requirement_id for item in ledger.requirements}
    for resolution in bundle.resolutions:
        if resolution.evidence is None:
            continue
        for requirement_id in resolution.request.requirement_ids:
            if requirement_id in known:
                by_requirement.setdefault(requirement_id, set()).add(
                    resolution.evidence.contract_id
                )

    requirements: list[dict] = []
    for requirement in ledger.requirements:
        payload = requirement.model_dump(mode="json")
        payload["required_contract_evidence_ids"] = sorted(
            {
                *requirement.required_contract_evidence_ids,
                *by_requirement.get(requirement.requirement_id, set()),
            }
        )
        requirements.append(payload)
    return _validated_copy(ledger, requirements)


def map_implementation_evidence(
    ledger: RequirementLedger,
    changed_paths: list[str],
) -> RequirementLedger:
    """Map every active requirement to concrete changed paths before PR creation.

    This is an implementation mapping, not verification. GitHub CI and the
    semantic reviewer still decide whether the behavior is correct. When a
    requirement has no unambiguous path-name match, the complete changed-file
    set is retained rather than silently claiming a narrower mapping.
    """
    paths = sorted({path.strip().removeprefix("./") for path in changed_paths if path.strip()})
    if not paths:
        raise ValueError("implementation evidence requires at least one changed path")

    requirements: list[dict] = []
    for requirement in ledger.requirements:
        payload = requirement.model_dump(mode="json")
        if requirement.implementation_status is not ImplementationStatus.planned:
            requirements.append(payload)
            continue
        tokens = {
            token.casefold()
            for token in _TOKEN.findall(
                f"{requirement.observable_behavior} {requirement.implementable_scope}"
            )
            if len(token) >= 4
        }
        target_paths = {
            target.path.removeprefix("./") for target in requirement.likely_targets
        }
        matched = [
            path
            for path in paths
            if path in target_paths
            or any(token in path.casefold() for token in tokens)
        ]
        selected = matched or paths
        payload["implementation_status"] = ImplementationStatus.implemented.value
        payload["implementation_evidence"] = [
            ImplementationEvidence(
                kind=ImplementationEvidenceKind.changed,
                path=path,
                description=(
                    "Changed-file implementation mapping for this requirement; "
                    "GitHub CI and semantic review remain authoritative for behavior."
                ),
            ).model_dump(mode="json")
            for path in selected
        ]
        requirements.append(payload)
    return _validated_copy(ledger, requirements)
