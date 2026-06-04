"""External partner-feed transforms -> ``feeds_v2``.

Feeds are the clearest "custom event" case: an adapter ingests something from
*outside* APDL (an EDI X12 document, a partner CSV drop, a webhook), stores the
raw bytes in object storage, and hands the framework a canonical envelope whose
``payload`` is the parsed document plus a content-addressed pointer
(``source_uri`` + ``source_sha256``) back to the original.

:class:`_FeedTransform` handles the envelope + partner identity + source pointer;
a concrete feed only needs to declare its ``schema``. The pointer fields are read
from ``ctx.extra`` (where the adapter stamps them) falling back to the payload,
so the same transform works whether the adapter threads them through context or
inlines them.
"""

from __future__ import annotations

from typing import Any

from etl.base import BaseTransform, _json
from etl.context import EtlContext, Row
from etl.envelope import CanonicalEnvelope
from etl.registry import register_transform

FEEDS_V2_COLUMNS = (
    "_id", "_schema", "_project_id", "_idempotency_key", "_correlation_id",
    "_source", "_occurred_at", "_received_at",
    "sender_id", "receiver_id", "control_number",
    "source_uri", "source_sha256", "source_bytes",
    "payload", "parse_warnings",
)


class _FeedTransform(BaseTransform):
    """Shared mapping for every external feed going to ``feeds_v2``."""

    target_table = "feeds_v2"
    columns = FEEDS_V2_COLUMNS

    def build_row(
        self, env: CanonicalEnvelope, ctx: EtlContext, enrichment: dict[str, Any]
    ) -> Row:
        p = env.payload
        src = ctx.extra
        row = self.envelope_columns(env, ctx)
        row.update(
            {
                "sender_id": p.get("sender_id", ""),
                "receiver_id": p.get("receiver_id", ""),
                "control_number": p.get("control_number", ""),
                "source_uri": src.get("source_uri") or p.get("source_uri", ""),
                "source_sha256": src.get("source_sha256") or p.get("source_sha256", ""),
                "source_bytes": int(src.get("source_bytes") or p.get("source_bytes", 0)),
                "payload": _json(p),
                "parse_warnings": list(p.get("parse_warnings", [])),
            }
        )
        return row


@register_transform
class ShipmentCsvFeedTransform(_FeedTransform):
    """Example partner feed: a CSV shipment manifest dropped by a logistics partner.

    Ships as a worked example of the feed pattern — a real deployment scaffolds
    one transform per partner document type (``edi.x12.850@1``,
    ``edi.x12.810@1``, ...) the same way.
    """

    schema = "partner.shipments.csv@1"
    description = "Partner shipment CSV feed (partner.shipments.csv@1) -> feeds_v2."
