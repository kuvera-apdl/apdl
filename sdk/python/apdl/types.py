"""Core event types and identifiers used across the SDK."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SDK_VERSION = "0.1.0"
SDK_IDENTIFIER = f"python/{SDK_VERSION}"

EventType = Literal["track", "identify", "group", "page"]

FEATURE_FLAG_EXPOSURE_EVENT = "$feature_flag_exposure"


def generate_id() -> str:
    """Generates a random UUID v4, matching the JS SDK's ``generateId``."""
    return str(uuid.uuid4())


def utc_now_iso() -> str:
    """Returns the current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class IngestionEvent(BaseModel):
    """Canonical on-the-wire event accepted by the ingestion service.

    Field names mirror the ``IngestionEvent`` shape produced by the JS SDK's
    event queue so both SDKs are byte-compatible with ``POST /v1/events``.
    """

    model_config = ConfigDict(extra="forbid")

    event: str
    type: EventType
    anonymous_id: str | None = None
    user_id: str | None = None
    group_id: str | None = None
    properties: dict[str, Any] | None = None
    traits: dict[str, Any] | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=utc_now_iso)
    message_id: str = Field(default_factory=generate_id)
    session_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Serializes to a dict, omitting unset/``None`` optional fields."""
        return self.model_dump(exclude_none=True)


def default_context() -> dict[str, Any]:
    """The library marker attached to every event's ``context``."""
    return {"library": {"name": "apdl-python", "version": SDK_VERSION}}
