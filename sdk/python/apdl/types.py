"""Core event types and identifiers used across the SDK."""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ._version import __version__ as SDK_VERSION

SDK_IDENTIFIER = f"python/{SDK_VERSION}"

EventType = Literal["track", "identify", "group", "page"]

FEATURE_FLAG_EXPOSURE_EVENT = "$feature_flag_exposure"

# These are the public ingestion limits. Keep them aligned with
# services/ingestion/app/validation/json_contract.py and
# services/ingestion/app/models/schemas.py: accepting more in an SDK only moves
# a permanent rejection into the background delivery queue.
MAX_JSON_DEPTH = 10
MAX_JSON_CONTAINER_ENTRIES = 100
MAX_JSON_TOTAL_NODES = 1_000
MAX_PROPERTY_KEY_LENGTH = 256
MAX_STRING_PROPERTY_LENGTH = 8_192
MAX_EVENT_SERIALIZED_BYTES = 64 * 1024
MAX_REQUEST_SERIALIZED_BYTES = 512 * 1024
MAX_EVENT_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_EVENT_FUTURE_SKEW_SECONDS = 5 * 60

_RFC3339_UTC_PATTERN = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$"
)
_OPTIONAL_CONTEXT_FIELDS = frozenset({
    "library",
    "browser",
    "os",
    "device",
    "screen",
    "viewport",
    "page",
    "locale",
    "timezone",
    "referrer",
})
_OPTIONAL_EVENT_FIELDS = frozenset({
    "user_id",
    "anonymous_id",
    "group_id",
    "properties",
    "traits",
    "session_id",
})


def generate_id() -> str:
    """Generates a random UUID v4, matching the JS SDK's ``generateId``."""
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Returns the current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return _utc_now().isoformat().replace("+00:00", "Z")


class NamedVersionContext(BaseModel):
    """Canonical library, browser, or operating-system descriptor."""

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

    url: str = Field(max_length=4_096)
    title: str = Field(max_length=1_024)
    path: str = Field(max_length=2_048)
    search: str = Field(max_length=2_048)


class EventContext(BaseModel):
    """Strict portable context shared with JavaScript and Ingestion."""

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
    referrer: str | None = Field(default=None, max_length=4_096)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, value: Any) -> Any:
        if isinstance(value, dict):
            null_fields = sorted(
                field
                for field in _OPTIONAL_CONTEXT_FIELDS
                if field in value and value[field] is None
            )
            if null_fields:
                raise ValueError(
                    "optional context fields must be omitted rather than null: "
                    + ", ".join(null_fields)
                )
        return value


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
    context: EventContext = Field(default_factory=EventContext)
    timestamp: str = Field(default_factory=utc_now_iso)
    message_id: str = Field(default_factory=generate_id)
    session_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, value: Any) -> Any:
        if isinstance(value, dict):
            null_fields = sorted(
                field
                for field in _OPTIONAL_EVENT_FIELDS
                if field in value and value[field] is None
            )
            if null_fields:
                raise ValueError(
                    "optional event fields must be omitted rather than null: "
                    + ", ".join(null_fields)
                )
        return value

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        match = _RFC3339_UTC_PATTERN.fullmatch(value)
        if match is None or int(match.group(1)) < 1_000:
            raise ValueError(
                "timestamp must be RFC3339 UTC with zero to six fractional digits"
            )
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise ValueError("timestamp must be a valid RFC3339 value") from exc
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("timestamp must use canonical UTC notation")
        reference_time = _utc_now()
        if parsed < reference_time - timedelta(seconds=MAX_EVENT_AGE_SECONDS):
            raise ValueError(
                "timestamp must not be more than 7 days older than the SDK clock"
            )
        if parsed > reference_time + timedelta(
            seconds=MAX_EVENT_FUTURE_SKEW_SECONDS
        ):
            raise ValueError(
                "timestamp must not be more than 5 minutes ahead of the SDK clock"
            )
        return value

    @field_validator("properties")
    @classmethod
    def validate_properties(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return value
        for key, item in value.items():
            if len(key) > MAX_PROPERTY_KEY_LENGTH:
                raise ValueError(
                    "property key exceeds maximum length of "
                    f"{MAX_PROPERTY_KEY_LENGTH}"
                )
            if isinstance(item, str) and len(item) > MAX_STRING_PROPERTY_LENGTH:
                raise ValueError(
                    "string property value exceeds maximum length of "
                    f"{MAX_STRING_PROPERTY_LENGTH}"
                )
        return value

    @model_validator(mode="after")
    def validate_canonical_event(self) -> "IngestionEvent":
        if not self.user_id and not self.anonymous_id:
            raise ValueError("event requires user_id or anonymous_id")
        expected_name = {
            "identify": "identify",
            "group": "group",
            "page": "page",
        }.get(self.type)
        if expected_name is not None and self.event != expected_name:
            raise ValueError(f"{self.type} events require event={expected_name!r}")
        if self.type == "identify" and not self.user_id:
            raise ValueError("identify events require user_id")
        if self.type == "group" and not self.group_id:
            raise ValueError("group events require group_id")
        return self

    def to_payload(self) -> dict[str, Any]:
        """Serializes to a dict, omitting unset/``None`` optional fields."""
        return self.model_dump(exclude_none=True)


def default_context() -> dict[str, Any]:
    """The library marker attached to every event's ``context``."""
    return {"library": {"name": "apdl-python", "version": SDK_VERSION}}


def canonicalize_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Return a detached, canonical event after complete pre-queue validation.

    Pydantic owns the event envelope and timestamp contract. The recursive walk
    below owns JSON compatibility and resource bounds. Running both before the
    queue takes ownership guarantees that a caller mutation, cycle, non-finite
    number, or unsupported Python value cannot poison a later batch.
    """
    # Bound and type-check the caller-owned graph before Pydantic traverses or
    # copies it. This makes cycles and extreme nesting deterministic client
    # errors instead of serializer failures.
    _validate_canonical_json(event)
    try:
        canonical = IngestionEvent.model_validate(event, strict=True).to_payload()
    except ValidationError as exc:
        raise ValueError(f"APDL: invalid event payload: {exc}") from exc

    _validate_canonical_json(canonical)
    size = serialized_json_size(canonical)
    if size > MAX_EVENT_SERIALIZED_BYTES:
        raise ValueError(
            "APDL: event exceeds maximum serialized size of "
            f"{MAX_EVENT_SERIALIZED_BYTES} bytes"
        )
    return canonical


def serialized_json_size(value: Any) -> int:
    """Return UTF-8 bytes for the exact compact encoding used by httpx."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise ValueError("APDL: value is not canonical JSON") from exc
    return len(encoded)


def _validate_canonical_json(value: Any) -> None:
    active_containers: set[int] = set()
    node_count = 0

    def visit(current: Any, path: str, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > MAX_JSON_TOTAL_NODES:
            raise ValueError(
                "APDL: event exceeds maximum JSON node count of "
                f"{MAX_JSON_TOTAL_NODES}"
            )
        if depth > MAX_JSON_DEPTH:
            raise ValueError(
                f"APDL: JSON nesting at {path} exceeds maximum depth of "
                f"{MAX_JSON_DEPTH}"
            )

        current_type = type(current)
        if current is None or current_type in (bool, int):
            return
        if current_type is float:
            if not math.isfinite(current):
                raise ValueError(f"APDL: non-finite number at {path}")
            return
        if current_type is str:
            return
        if current_type not in (dict, list):
            raise ValueError(
                f"APDL: unsupported non-JSON value at {path}: "
                f"{current_type.__name__}"
            )
        if len(current) > MAX_JSON_CONTAINER_ENTRIES:
            raise ValueError(
                f"APDL: JSON container at {path} exceeds maximum cardinality of "
                f"{MAX_JSON_CONTAINER_ENTRIES}"
            )

        identity = id(current)
        if identity in active_containers:
            raise ValueError(f"APDL: cyclic JSON value at {path}")
        active_containers.add(identity)
        try:
            if current_type is dict:
                for key, child in current.items():
                    if type(key) is not str:
                        raise ValueError(f"APDL: non-string JSON object key at {path}")
                    visit(child, f"{path}.{key}", depth + 1)
            else:
                for index, child in enumerate(current):
                    visit(child, f"{path}[{index}]", depth + 1)
        finally:
            active_containers.remove(identity)

    # Match Ingestion exactly: the top-level event object is depth zero and
    # every child value consumes one level.
    visit(value, "$", 0)
