"""Bounded repository inspection and changed-file dependency slicing."""

from app.inspection.models import (
    DependencySlice,
    EvidenceKind,
    EvidenceRef,
    InspectionSnapshot,
)
from app.inspection.preflight import RepositoryPreflightAttestation
from app.inspection.repository import InspectionPathError, RepositoryInspector
from app.inspection.render import render_dependency_slice, render_inspection_snapshot
from app.inspection.slice import build_dependency_slice

__all__ = [
    "DependencySlice",
    "EvidenceKind",
    "EvidenceRef",
    "InspectionPathError",
    "InspectionSnapshot",
    "RepositoryPreflightAttestation",
    "RepositoryInspector",
    "build_dependency_slice",
    "render_dependency_slice",
    "render_inspection_snapshot",
]
