"""Canonical envelope + behavior-event payloads for the ingestion service.

Every record entering APDL — events, decisions, agent actions, external feeds —
is wrapped in the same outer envelope. This file is the boundary contract for
behavioral events emitted by the SDK. Decision and feed envelopes live in
their respective services.

The envelope rejects unknown top-level keys (extra="forbid"). Anything the SDK
sends outside the contract goes to the events_dlq_v2 dead-letter table so we
keep an audit trail of bad data without polluting the canonical store.

Wire shape (JSON):

    {
      "_id":              "<uuid>",
      "_schema":          "track@1",
      "_project_id":      42,
      "_idempotency_key": "<sdk-messageId>",
      "_correlation_id":  "<uuid>",
      "_source":          "sdk-js@2.4.1",
      "_occurred_at":     "2026-05-26T10:11:12.345Z",
      "payload":          { ... per-schema body ... }
    }

`_received_at`, `_ingested_at`, and `_ip` are added server-side and are not
part of the SDK-supplied envelope.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .schemas import (
    MAX_BATCH_SIZE,
    MAX_EVENT_NAME_LENGTH,
    MAX_PROPERTY_KEY_LENGTH,
    MAX_STRING_PROPERTY_LENGTH,
)

# ---------- context (typed, not free-form) ----------

class _NameVersion(BaseModel):
    """Shared shape for the name+version context blocks (browser, OS, library)."""

    model_config = ConfigDict(extra="forbid")
    name: str = ""
    version: str = ""


class BrowserContext(_NameVersion):
    pass


class OSContext(_NameVersion):
    pass


class DeviceContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = ""                                 # 'mobile' | 'tablet' | 'desktop'


class PageContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = ""
    title: str = ""
    path: str = ""
    search: str = ""
    referrer: str = ""


class LibraryContext(_NameVersion):
    pass


class GeoContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country: str = ""
    region: str = ""


class EventContext(BaseModel):
    """Structured context attached to every event."""

    model_config = ConfigDict(extra="forbid")

    browser: BrowserContext = Field(default_factory=BrowserContext)
    os: OSContext = Field(default_factory=OSContext)
    device: DeviceContext = Field(default_factory=DeviceContext)
    page: PageContext = Field(default_factory=PageContext)
    library: LibraryContext = Field(default_factory=LibraryContext)
    geo: GeoContext = Field(default_factory=GeoContext)
    locale: str = ""
    timezone: str = ""


# ---------- per-type payloads ----------

class _PayloadBase(BaseModel):
    """Fields shared by every behavioral-event payload."""

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None
    anonymous_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(default="", max_length=128)
    context: EventContext = Field(default_factory=EventContext)
    properties: dict[str, Any] = Field(default_factory=dict)
    # `properties` is the ONE place free-form data is allowed. The validator
    # below enforces per-key/value length so a single huge property cannot
    # blow out a ClickHouse row.


def _check_properties(props: dict[str, Any]) -> dict[str, Any]:
    for key, value in props.items():
        if len(key) > MAX_PROPERTY_KEY_LENGTH:
            raise ValueError(f"property key '{key[:32]}...' exceeds {MAX_PROPERTY_KEY_LENGTH} chars")
        if isinstance(value, str) and len(value) > MAX_STRING_PROPERTY_LENGTH:
            raise ValueError(f"property '{key}' string value exceeds {MAX_STRING_PROPERTY_LENGTH} chars")
    return props


class TrackPayload(_PayloadBase):
    event: str = Field(min_length=1, max_length=MAX_EVENT_NAME_LENGTH)


class PagePayload(_PayloadBase):
    name: str = Field(default="", max_length=MAX_EVENT_NAME_LENGTH)
    category: str = Field(default="", max_length=MAX_EVENT_NAME_LENGTH)


class ScreenPayload(_PayloadBase):
    name: str = Field(default="", max_length=MAX_EVENT_NAME_LENGTH)
    category: str = Field(default="", max_length=MAX_EVENT_NAME_LENGTH)


class IdentifyPayload(_PayloadBase):
    traits: dict[str, Any] = Field(default_factory=dict)


class GroupPayload(_PayloadBase):
    group_id: str = Field(min_length=1, max_length=128)
    traits: dict[str, Any] = Field(default_factory=dict)


class AliasPayload(_PayloadBase):
    previous_id: str = Field(min_length=1, max_length=128)


# ---------- envelope ----------

# Discriminated union: Pydantic uses the parent envelope's `_schema` value to
# pick the right payload class. Adding a new event type = add a payload class
# + a Literal entry here.
_SchemaLiteral = Literal[
    "track@1",
    "page@1",
    "screen@1",
    "identify@1",
    "group@1",
    "alias@1",
]

# Maps the envelope's `_schema` discriminator to its payload class. The union
# below stays undiscriminated (the discriminator lives on the envelope, not the
# payload), so this is the single source of truth for the schema→payload pairing.
_SCHEMA_TO_PAYLOAD: dict[str, type[_PayloadBase]] = {
    "track@1": TrackPayload,
    "page@1": PagePayload,
    "screen@1": ScreenPayload,
    "identify@1": IdentifyPayload,
    "group@1": GroupPayload,
    "alias@1": AliasPayload,
}


class EventEnvelope(BaseModel):
    """The canonical wire shape for a single inbound behavioral event."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # All envelope keys use the on-the-wire `_` prefix via alias; Python
    # attribute names stay PEP-8.
    id: UUID = Field(alias="_id")
    schema_: _SchemaLiteral = Field(alias="_schema")
    project_id: int = Field(alias="_project_id", ge=1)
    idempotency_key: str = Field(alias="_idempotency_key", min_length=1, max_length=128)
    correlation_id: UUID | None = Field(default=None, alias="_correlation_id")
    source: str = Field(alias="_source", min_length=1, max_length=64)
    occurred_at: datetime = Field(alias="_occurred_at")

    # Undiscriminated union: the discriminator lives on the envelope's `_schema`.
    # `validate_payload_shape()` cross-checks the parsed payload against it.
    payload: (
        TrackPayload
        | PagePayload
        | ScreenPayload
        | IdentifyPayload
        | GroupPayload
        | AliasPayload
    )

    def validate_payload_shape(self) -> None:
        """Cross-field check: payload class must match the schema discriminator."""
        expected = _SCHEMA_TO_PAYLOAD[self.schema_]
        if not isinstance(self.payload, expected):
            raise ValueError(
                f"_schema={self.schema_} does not match payload type {type(self.payload).__name__}"
            )
        _check_properties(self.payload.properties)


class EventEnvelopeBatch(BaseModel):
    """Batched POST body: { envelopes: [...] }."""

    model_config = ConfigDict(extra="forbid")
    envelopes: list[EventEnvelope] = Field(min_length=1, max_length=MAX_BATCH_SIZE)


# ---------- server-side enrichment ----------

class EnrichedEnvelope(BaseModel):
    """Envelope plus fields added by the ingestion server before publishing
    to Redis Streams. This is the shape the ClickHouse writer reads from
    the stream."""

    model_config = ConfigDict(extra="forbid")

    envelope: EventEnvelope
    received_at: datetime
    ingested_at: datetime | None = None
    ip: str = ""                                   # IPv4 or IPv6 in canonical form
