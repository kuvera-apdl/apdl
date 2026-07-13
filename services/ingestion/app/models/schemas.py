"""Strict canonical models for the public event-ingestion contract."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_BATCH_SIZE = 100
MAX_EVENT_NAME_LENGTH = 256
MAX_PROPERTY_KEY_LENGTH = 256
MAX_STRING_PROPERTY_LENGTH = 8192
VALID_EVENT_TYPES = frozenset({"track", "identify", "group", "page"})
CANONICAL_EVENT_NAMES = {
    "identify": "identify",
    "group": "group",
    "page": "page",
}


class NamedVersionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)


class DeviceContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=64)


class DimensionsContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(ge=0, le=100_000)
    height: int = Field(ge=0, le=100_000)


class PageContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(max_length=4096)
    title: str = Field(max_length=1024)
    path: str = Field(max_length=2048)
    search: str = Field(max_length=2048)


class EventContext(BaseModel):
    """Portable nested context shared by browser and server SDKs."""

    model_config = ConfigDict(extra="forbid")

    library: NamedVersionContext | None = None
    browser: NamedVersionContext | None = None
    os: NamedVersionContext | None = None
    device: DeviceContext | None = None
    screen: DimensionsContext | None = None
    viewport: DimensionsContext | None = None
    page: PageContext | None = None
    locale: str | None = Field(default=None, max_length=128)
    timezone: str | None = Field(default=None, max_length=128)
    referrer: str | None = Field(default=None, max_length=4096)


class ValidationError(BaseModel):
    field: str
    message: str


class ValidationResult(BaseModel):
    valid: bool
    errors: list[ValidationError] = []


class Event(BaseModel):
    """One canonical SDK-to-Ingestion event.

    Every producer sends this snake_case shape.  There are intentionally no
    camelCase aliases or optional envelope fallbacks: accepting competing wire
    shapes at the public edge previously let SDKs and the writer drift apart.
    """

    model_config = ConfigDict(extra="forbid")

    event: str
    type: str
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    anonymous_id: str | None = Field(default=None, min_length=1, max_length=128)
    group_id: str | None = Field(default=None, min_length=1, max_length=128)
    timestamp: str
    properties: dict[str, Any] | None = None
    traits: dict[str, Any] | None = None
    context: EventContext
    message_id: str = Field(min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("event")
    @classmethod
    def validate_event_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if len(v) == 0:
            raise ValueError("Event name must not be empty")
        if len(v) > MAX_EVENT_NAME_LENGTH:
            raise ValueError(f"Event name exceeds maximum length of {MAX_EVENT_NAME_LENGTH}")
        return v

    @field_validator("type")
    @classmethod
    def validate_event_type(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_EVENT_TYPES:
            raise ValueError(
                f"Invalid event type '{v}'. Must be one of: track, identify, group, page"
            )
        return v

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        if not value.endswith("Z"):
            raise ValueError("Timestamp must be RFC3339 UTC with a 'Z' suffix")
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise ValueError("Timestamp must be a valid RFC3339 value") from exc
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("Timestamp must use UTC")
        return value

    @model_validator(mode="after")
    def validate_canonical_envelope(self) -> "Event":
        if not self.user_id and not self.anonymous_id:
            raise ValueError("Event requires user_id or anonymous_id")

        canonical_name = CANONICAL_EVENT_NAMES.get(self.type)
        if canonical_name is not None and self.event != canonical_name:
            raise ValueError(
                f"Event type '{self.type}' requires event='{canonical_name}'"
            )
        if self.type == "identify" and not self.user_id:
            raise ValueError("Identify events require user_id")
        if self.type == "group" and not self.group_id:
            raise ValueError("Group events require group_id")
        return self

    @field_validator("properties")
    @classmethod
    def validate_properties(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return v
        for key, value in v.items():
            if len(key) > MAX_PROPERTY_KEY_LENGTH:
                raise ValueError(f"Property key exceeds maximum length of {MAX_PROPERTY_KEY_LENGTH}")
            if isinstance(value, str) and len(value) > MAX_STRING_PROPERTY_LENGTH:
                raise ValueError(f"String property value exceeds maximum length of {MAX_STRING_PROPERTY_LENGTH}")
        return v


class EventBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[Event] = Field(..., min_length=1)
