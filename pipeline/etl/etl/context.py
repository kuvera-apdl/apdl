"""Shared value objects for the ETL framework.

These are the small, dependency-free dataclasses every other module passes
around: the server-side metadata bundle (:class:`EtlContext`), the result of a
transform (:class:`TransformResult`), and a dead-letter record (:class:`DlqEntry`).

A ``Row`` is just a ``dict`` of ClickHouse column name -> value. The framework
never builds SQL itself; it produces rows and hands them to a loader, which is
the seam a real warehouse writer plugs into.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: A single output row: ClickHouse column name -> value.
Row = dict[str, Any]

#: Canonical "no correlation id" sentinel, matching the ClickHouse default for
#: the non-nullable ``_correlation_id`` / ``run_id`` UUID columns.
ZERO_UUID = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class EtlContext:
    """Server-side metadata stamped onto a record before it reaches the warehouse.

    These fields are supplied by the ingestion / landing layer, never by the
    client envelope — keeping them out of the envelope is what lets the same
    transform run for replays, backfills, and live traffic unchanged.

    ``extra`` is a free-form bag for adapters that need to thread additional
    context (e.g. a feed's ``source_uri`` + ``source_sha256``) without having to
    subclass the context.
    """

    project_id: int
    received_at: datetime
    ingested_at: datetime | None = None
    ip: str = ""
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DlqEntry:
    """A record that failed decode / validate / transform, bound for the DLQ table.

    The shape mirrors ``events_dlq_v2`` so analysts can investigate bad data in
    SQL without leaving the warehouse.
    """

    project_id: int
    source: str
    error: str
    raw_payload: str
    table: str = "events_dlq_v2"


@dataclass
class TransformResult:
    """What a transform produces for a single input record.

    Exactly one of ``rows`` (success) or ``dlq`` (failure) is meaningful;
    :attr:`ok` distinguishes them.
    """

    target: str
    rows: list[Row] = field(default_factory=list)
    dlq: DlqEntry | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the record transformed cleanly (no DLQ entry)."""
        return self.dlq is None
