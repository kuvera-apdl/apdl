"""The canonical envelope every record shares.

APDL wraps everything entering the warehouse — behavioral events, decisions,
external partner feeds — in one outer envelope keyed by a ``_schema``
discriminator. The v2 ClickHouse tables (``events_v2``, ``decisions_v2``,
``feeds_v2``) are all built around it.

This model is deliberately decoupled from the ingestion service's Pydantic
models: the ETL package owns a *minimal* envelope contract so it stands alone
and can be reused by any producer (SDK events, a config-service decision
emitter, an EDI feed adapter). The ``_received_at`` / ``_ingested_at`` / ``_ip``
columns are server-side and travel in :class:`~etl.context.EtlContext`, not in
the envelope itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CanonicalEnvelope(BaseModel):
    """Common outer shape. ``payload`` stays free-form per ``schema_``.

    The envelope rejects unknown top-level keys (``extra="forbid"``) so a
    malformed producer is caught at decode time and routed to the DLQ rather
    than silently dropping fields. Wire keys carry the on-the-wire ``_`` prefix
    via alias; Python attribute names stay PEP-8.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: UUID = Field(alias="_id")
    schema_: str = Field(alias="_schema", min_length=1, max_length=64)
    project_id: str = Field(
        alias="_project_id",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9]+$",
    )
    idempotency_key: str = Field(alias="_idempotency_key", min_length=1, max_length=128)
    correlation_id: UUID | None = Field(default=None, alias="_correlation_id")
    source: str = Field(alias="_source", min_length=1, max_length=64)
    occurred_at: datetime = Field(alias="_occurred_at")
    payload: dict[str, Any] = Field(default_factory=dict)
