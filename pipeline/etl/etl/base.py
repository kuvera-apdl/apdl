"""BaseTransform — the Template Method for the unsupported ETL prototype.

Every prototype record runs the same lifecycle::

    decode  ->  validate  ->  enrich  ->  build_row    (-> load, done by the pipeline)
    (parse)     (reject)      (derive)    (map to rows)

``BaseTransform`` implements that skeleton once in :meth:`process` and exposes
the varying parts as overridable hooks. The invariant concerns — envelope
validation, the enricher chain, and error isolation (any exception becomes a
DLQ entry instead of crashing the batch) — live in the base class, so a new
custom event type is a small subclass plus, at most, a payload model.

Declarative class attributes configure the fixed parts; hook methods supply the
behaviour::

    @register_transform
    class RefundIssuedTransform(BaseTransform):
        schema = "refund.issued@1"
        target_table = "events_v2"
        enrichers = ("device", "geo")

        def build_row(self, env, ctx, enrichment):
            row = self.envelope_columns(env, ctx)
            row["event_name"] = "refund_issued"
            ...
            return row
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel

from etl.context import ZERO_UUID, DlqEntry, EtlContext, Row, TransformResult
from etl.enrichment import run_enrichers
from etl.envelope import CanonicalEnvelope

logger = logging.getLogger(__name__)


def _json(value: Any) -> str:
    """Compact, deterministic JSON for a String column."""
    return json.dumps(value, separators=(",", ":"), default=str, sort_keys=True)


def _raw_json(raw: Any) -> str:
    return _json(raw) if not isinstance(raw, str) else raw


def _raw_source(raw: Any) -> str:
    return raw.get("_source", "") if isinstance(raw, dict) else ""


class BaseTransform(ABC):
    """Template Method base class for all ETL transforms."""

    # --- declarative configuration (subclasses set these) -------------------
    #: The ``_schema`` discriminator this transform handles (registry key).
    schema: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: Destination ClickHouse table (e.g. "events_v2", "decisions_v2").
    target_table: ClassVar[str] = ""
    #: Where failures land. Same shape across tables, so one DLQ table is fine.
    dlq_table: ClassVar[str] = "events_dlq_v2"
    #: Pydantic model used to decode/validate the raw dict. ``None`` to skip.
    envelope_model: ClassVar[type[BaseModel] | None] = CanonicalEnvelope
    #: Names of registered enrichers to run, in order, before build_row.
    enrichers: ClassVar[tuple[str, ...]] = ()
    #: Declared output columns — used by loaders to build a stable INSERT and as
    #: living documentation. Empty means "whatever build_row returns".
    columns: ClassVar[tuple[str, ...]] = ()

    # --- lifecycle hooks (override as needed) -------------------------------

    def decode(self, raw: Any, ctx: EtlContext) -> Any:
        """Parse the raw input into a validated envelope.

        Defaults to validating the raw dict against :attr:`envelope_model`.
        Override for non-JSON sources (parse EDI/CSV into the prototype envelope
        dict first, then defer to ``super().decode``).
        """
        if self.envelope_model is None:
            return raw
        return self.envelope_model.model_validate(raw)

    def validate(self, envelope: Any, ctx: EtlContext) -> None:
        """Cross-field checks beyond what the envelope model enforces.

        Raise to reject the record (it is routed to the DLQ). Default: no-op.
        """

    def enrich(self, envelope: Any, ctx: EtlContext) -> dict[str, Any]:
        """Run the declared enricher chain. Override to add bespoke derivation."""
        return run_enrichers(self.enrichers, envelope, ctx)

    @abstractmethod
    def build_row(
        self, envelope: Any, ctx: EtlContext, enrichment: dict[str, Any]
    ) -> Row | list[Row]:
        """Map the validated, enriched envelope to one or more warehouse rows."""

    # --- template method ----------------------------------------------------

    def process(self, raw: Any, ctx: EtlContext) -> TransformResult:
        """Run the full transform lifecycle. Do not override — override hooks.

        Any exception in decode/validate/enrich/build_row is isolated and turned
        into a :class:`DlqEntry` so one poison record never sinks a batch.
        """
        try:
            envelope = self.decode(raw, ctx)
            self.validate(envelope, ctx)
            enrichment = self.enrich(envelope, ctx)
            built = self.build_row(envelope, ctx, enrichment)
            rows = [built] if isinstance(built, dict) else list(built)
            return TransformResult(target=self.target_table, rows=rows)
        except Exception as exc:
            logger.warning("[%s] transform failed: %s", self.schema, exc)
            return TransformResult(
                target=self.target_table,
                dlq=DlqEntry(
                    project_id=getattr(ctx, "project_id", 0),
                    source=getattr(ctx, "source", "") or _raw_source(raw),
                    error=f"{type(exc).__name__}: {exc}",
                    raw_payload=_raw_json(raw),
                    table=self.dlq_table,
                ),
            )

    # --- framework-provided helpers -----------------------------------------

    def envelope_columns(self, env: CanonicalEnvelope, ctx: EtlContext) -> Row:
        """The common ``_``-prefixed columns shared by the prototype v2 tables.

        Subclasses start their row from this and add payload-specific columns
        (and ``_ip`` for the events table, which is the only one that keeps it).
        """
        return {
            "_id": str(env.id),
            "_schema": env.schema_,
            "_project_id": ctx.project_id,
            "_idempotency_key": env.idempotency_key,
            "_correlation_id": str(env.correlation_id) if env.correlation_id else ZERO_UUID,
            "_source": env.source,
            "_occurred_at": env.occurred_at,
            "_received_at": ctx.received_at,
        }
