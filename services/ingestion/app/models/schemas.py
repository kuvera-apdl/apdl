"""Pydantic models for event ingestion."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_BATCH_SIZE = 500
MAX_EVENT_NAME_LENGTH = 256
MAX_PROPERTY_KEY_LENGTH = 256
MAX_STRING_PROPERTY_LENGTH = 8192
VALID_EVENT_TYPES = frozenset({"track", "identify", "group", "page", "screen", "alias"})


class ValidationError(BaseModel):
    field: str
    message: str


class ValidationResult(BaseModel):
    valid: bool
    errors: list[ValidationError] = []


class Event(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    event: str | None = None
    type: str | None = None
    user_id: str | None = None
    anonymous_id: str | None = None
    userId: str | None = None
    anonymousId: str | None = None
    timestamp: str | None = None
    properties: dict[str, Any] | None = None
    traits: dict[str, Any] | None = None
    context: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def require_event_or_type(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if not isinstance(data.get("event"), str) and not isinstance(data.get("type"), str):
            raise ValueError("Event must have either 'event' (name) or 'type' field")
        return data

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
                f"Invalid event type '{v}'. Must be one of: track, identify, group, page, screen, alias"
            )
        return v

    @model_validator(mode="after")
    def require_user_identity(self) -> "Event":
        has_user = bool(self.user_id) or bool(self.userId)
        has_anon = bool(self.anonymous_id) or bool(self.anonymousId)
        if not has_user and not has_anon:
            raise ValueError(
                "Event must have either 'user_id'/'userId' or 'anonymous_id'/'anonymousId'"
            )
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
    events: list[Event] = Field(..., min_length=1, max_length=MAX_BATCH_SIZE)
