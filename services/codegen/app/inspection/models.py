"""Strict evidence contracts for bounded repository inspection.

The inspection layer never hands arbitrary repository contents to a model.
Instead it emits content-addressed references whose paths, locations, and
relationships can be audited independently of the editor implementation.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    """Base model for canonical inspection contracts."""

    model_config = ConfigDict(extra="forbid")


class EvidenceKind(str, Enum):
    file = "file"
    search = "search"
    symbol = "symbol"
    local_import = "local_import"
    caller = "caller"
    route = "route"
    link = "link"
    test = "test"
    lockfile = "lockfile"
    contract = "contract"
    config = "config"


def _relative_path(value: str) -> str:
    normalized = value.replace("\\", "/").removeprefix("./")
    parts = normalized.split("/")
    if not normalized or normalized.startswith("/") or ".." in parts:
        raise ValueError("evidence paths must be non-empty repository-relative paths")
    return normalized


class EvidenceRef(StrictModel):
    """One content-addressed repository fact.

    ``path`` is always the file containing the primary evidence. Relationship
    evidence may additionally identify where it originated (``source_path``)
    or what it points to (``target_path``).
    """

    evidence_id: str = Field(pattern=r"^ev_[0-9a-f]{24}$")
    kind: EvidenceKind
    path: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    source_path: str | None = None
    source_line: int | None = Field(default=None, ge=1)
    target_path: str | None = None
    symbol: str | None = None
    excerpt: str | None = Field(default=None, max_length=4000)
    truncated: bool = False

    @field_validator("path", "source_path", "target_path")
    @classmethod
    def validate_paths(cls, value: str | None) -> str | None:
        return _relative_path(value) if value is not None else None

    @model_validator(mode="after")
    def validate_lines(self) -> EvidenceRef:
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("start_line and end_line must be provided together")
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("end_line must not precede start_line")
        if self.source_line is not None and self.source_path is None:
            raise ValueError("source_line requires source_path")
        return self


class InspectionSnapshot(StrictModel):
    """Bounded, deterministic inventory of safe-to-inspect repository files."""

    schema_version: Literal["inspection_snapshot@1"] = "inspection_snapshot@1"
    root_label: Literal["."] = "."
    evidence: list[EvidenceRef] = Field(default_factory=list)
    skipped_paths: list[str] = Field(default_factory=list)
    bytes_inspected: int = Field(default=0, ge=0)
    truncated: bool = False

    @field_validator("skipped_paths")
    @classmethod
    def validate_skipped_paths(cls, values: list[str]) -> list[str]:
        return [_relative_path(value) for value in values]

    @model_validator(mode="after")
    def unique_evidence(self) -> InspectionSnapshot:
        ids = [item.evidence_id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("inspection evidence IDs must be unique")
        return self


class DependencySlice(StrictModel):
    """Evidence reachable from a proposed change, grouped by relationship."""

    schema_version: Literal["dependency_slice@1"] = "dependency_slice@1"
    changed_files: list[EvidenceRef] = Field(default_factory=list)
    imported_local_symbols: list[EvidenceRef] = Field(default_factory=list)
    callers: list[EvidenceRef] = Field(default_factory=list)
    routes_and_handlers: list[EvidenceRef] = Field(default_factory=list)
    affected_tests: list[EvidenceRef] = Field(default_factory=list)
    relevant_lockfiles: list[EvidenceRef] = Field(default_factory=list)
    external_contracts: list[EvidenceRef] = Field(default_factory=list)
    unresolved_references: list[str] = Field(default_factory=list)
    truncated: bool = False

    @model_validator(mode="after")
    def unique_group_evidence(self) -> DependencySlice:
        for name in (
            "changed_files",
            "imported_local_symbols",
            "callers",
            "routes_and_handlers",
            "affected_tests",
            "relevant_lockfiles",
            "external_contracts",
        ):
            values = getattr(self, name)
            ids = [item.evidence_id for item in values]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{name} evidence IDs must be unique")
        return self
