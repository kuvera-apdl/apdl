"""Focused tests for bounded, secret-aware repository inspection."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.inspection.models import DependencySlice, EvidenceRef, InspectionSnapshot
from app.inspection.repository import InspectionPathError, RepositoryInspector


def _write(root: Path, path: str, content: str | bytes) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")


def test_snapshot_is_stable_strict_and_excludes_secrets_and_binary(tmp_path: Path):
    _write(tmp_path, "src/app.py", "def launch():\n    return 'ok'\n")
    _write(tmp_path, "src/config.py", "TOKEN = 'ghp_" + "a" * 40 + "'\n")
    _write(tmp_path, ".env", "PASSWORD=do-not-read\n")
    _write(tmp_path, "cert.pem", "-----BEGIN PRIVATE KEY-----\nsecret\n")
    _write(tmp_path, "assets/logo.png", b"\x89PNG\x00binary")

    inspector = RepositoryInspector(tmp_path)
    first = inspector.snapshot()
    second = inspector.snapshot()

    assert first == second
    assert [item.path for item in first.evidence] == ["src/app.py"]
    assert first.evidence[0].evidence_id.startswith("ev_")
    assert first.skipped_paths == [
        ".env",
        "assets/logo.png",
        "cert.pem",
        "src/config.py",
    ]
    assert all("do-not-read" not in (item.excerpt or "") for item in first.evidence)

    with pytest.raises(ValidationError):
        EvidenceRef.model_validate(
            {**first.evidence[0].model_dump(mode="json"), "unknown": True}
        )
    with pytest.raises(ValidationError):
        InspectionSnapshot.model_validate(
            {**first.model_dump(mode="json"), "unknown": True}
        )
    with pytest.raises(ValidationError):
        DependencySlice.model_validate(
            {**DependencySlice().model_dump(mode="json"), "unknown": True}
        )


def test_focused_read_and_search_are_bounded_and_content_addressed(tmp_path: Path):
    _write(
        tmp_path,
        "src/service.py",
        "class Widget:\n    pass\n\nWidget()\nWidget()\n",
    )
    _write(tmp_path, "src/other.py", "Widget = 'different file'\n")
    inspector = RepositoryInspector(tmp_path, max_search_results=2)

    focused = inspector.read("src/service.py", start_line=1, end_line=2)
    assert focused.start_line == 1
    assert focused.end_line == 2
    assert focused.excerpt == "class Widget:\n    pass"

    results = inspector.search("Widget", symbol=True, max_results=20)
    assert len(results) == 2
    assert all(result.symbol == "Widget" for result in results)
    assert results == inspector.search("Widget", symbol=True, max_results=20)


def test_inspection_rejects_traversal_symlinks_and_secret_reads(tmp_path: Path):
    _write(tmp_path, "safe.txt", "safe\n")
    _write(tmp_path, ".env.local", "TOKEN=hidden\n")
    outside = tmp_path.parent / "outside-inspection.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)

    inspector = RepositoryInspector(tmp_path)
    with pytest.raises(InspectionPathError):
        inspector.read("../outside-inspection.txt")
    with pytest.raises(InspectionPathError):
        inspector.read("linked.txt")
    with pytest.raises(InspectionPathError):
        inspector.read(".env.local")


def test_large_file_reads_are_truncated_without_crossing_budget(tmp_path: Path):
    _write(tmp_path, "large.txt", "abcdef\n" * 100)
    inspector = RepositoryInspector(
        tmp_path, max_file_bytes=64, max_total_bytes=64, max_files=10
    )

    evidence = inspector.read("large.txt")
    snapshot = inspector.snapshot()

    assert evidence.truncated is True
    assert len((evidence.excerpt or "").encode()) <= 64
    assert snapshot.bytes_inspected == 64
    assert snapshot.evidence[0].truncated is True


def test_exact_file_budget_and_empty_focused_read_are_not_false_truncations(
    tmp_path: Path,
):
    _write(tmp_path, "empty.txt", "")
    inspector = RepositoryInspector(tmp_path, max_files=1)

    snapshot = inspector.snapshot()
    focused = inspector.read("empty.txt")

    assert snapshot.truncated is False
    assert focused.start_line == focused.end_line == 1
    assert focused.excerpt == ""
    with pytest.raises(ValueError):
        inspector.search("empty", max_results=0)
