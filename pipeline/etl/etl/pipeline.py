"""EtlPipeline — dispatches records to transforms and routes their output.

This is the public entry point a warehouse writer calls. For each record it:

1. reads the ``_schema`` discriminator,
2. resolves the registered transform (unrouted schemas go straight to the DLQ),
3. runs the transform's lifecycle, and
4. routes the resulting rows to the loader, or the failure to the DLQ loader.

The pipeline owns no warehouse knowledge — it talks to the registry and a
:class:`~etl.loader.Loader`. A real writer constructs one with a ClickHouse-backed
loader and feeds it records off the stream; the framework stays testable with an
in-memory :class:`~etl.loader.CollectingLoader`.
"""

from __future__ import annotations

import logging
from typing import Any

from etl.base import _raw_json, _raw_source
from etl.context import DlqEntry, EtlContext, Row, TransformResult
from etl.loader import Loader
from etl.registry import get_transform, is_registered

logger = logging.getLogger(__name__)


def dlq_row(entry: DlqEntry) -> Row:
    """Map a :class:`DlqEntry` to an ``events_dlq_v2`` row.

    ``_received_at`` is left to the table's ``DEFAULT now64(3)``.
    """
    return {
        "_project_id": entry.project_id,
        "_source": entry.source,
        "error": entry.error,
        "raw_payload": entry.raw_payload,
    }


class EtlPipeline:
    """Schema-routed dispatcher over the transform registry."""

    def __init__(self, loader: Loader, dlq_loader: Loader | None = None) -> None:
        self.loader = loader
        #: Failures can go to a separate loader; defaults to the main one.
        self.dlq_loader = dlq_loader or loader
        self.stats = {"processed": 0, "rows": 0, "dlq": 0, "unrouted": 0}

    def process_record(self, raw: Any, ctx: EtlContext) -> TransformResult:
        """Route, transform, and load a single record."""
        self.stats["processed"] += 1
        schema = raw.get("_schema") if isinstance(raw, dict) else None

        if not schema or not is_registered(schema):
            self.stats["unrouted"] += 1
            return self._to_dlq(
                DlqEntry(
                    project_id=getattr(ctx, "project_id", 0),
                    source=getattr(ctx, "source", "") or _raw_source(raw),
                    error=f"no transform registered for _schema={schema!r}",
                    raw_payload=_raw_json(raw),
                )
            )

        result = get_transform(schema).process(raw, ctx)
        if result.dlq is not None:
            self._to_dlq(result.dlq)
        else:
            self.loader.load(result.target, result.rows)
            self.stats["rows"] += len(result.rows)
        return result

    def process_batch(
        self, records: list[Any], ctx: EtlContext
    ) -> list[TransformResult]:
        """Process a batch sharing one context. Returns per-record results."""
        return [self.process_record(raw, ctx) for raw in records]

    def _to_dlq(self, entry: DlqEntry) -> TransformResult:
        self.dlq_loader.load(entry.table, [dlq_row(entry)])
        self.stats["dlq"] += 1
        return TransformResult(target=entry.table, dlq=entry)
