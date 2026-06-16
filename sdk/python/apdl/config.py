"""Client configuration model with validation and sensible defaults."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

DEFAULT_ENDPOINT = "https://api.apdl.dev"
DEFAULT_BATCH_SIZE = 20
MAX_BATCH_SIZE = 100
DEFAULT_FLUSH_INTERVAL = 3.0
DEFAULT_MAX_QUEUE_SIZE = 1000
DEFAULT_FLAG_POLL_INTERVAL = 30.0
DEFAULT_REQUEST_TIMEOUT = 10.0


class APDLConfig(BaseModel):
    """Resolved, validated SDK configuration.

    Mirrors the JS SDK's ``APDLConfig``/``resolveConfig`` where it makes sense
    for a server-side client. Browser-only concepts (persistence, consent,
    auto-capture, UI) are intentionally omitted.

    A single ``endpoint`` origin serves both event ingestion (``/v1/events``)
    and flag config (``/v1/flags``); a gateway routes each path to the right
    service behind that origin.
    """

    model_config = ConfigDict(extra="forbid")

    api_key: str
    endpoint: str = DEFAULT_ENDPOINT

    # Event batching
    batch_size: int = DEFAULT_BATCH_SIZE
    flush_interval: float = DEFAULT_FLUSH_INTERVAL
    max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE

    # Feature flags
    enable_flags: bool = True
    flag_poll_interval: float = DEFAULT_FLAG_POLL_INTERVAL
    log_exposures: bool = True

    # Transport
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    debug: bool = False

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("apiKey is required and must be a non-empty string")
        return v

    @field_validator("endpoint")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("batch_size")
    @classmethod
    def _clamp_batch_size(cls, v: int) -> int:
        return max(1, min(v, MAX_BATCH_SIZE))

    @field_validator("max_queue_size")
    @classmethod
    def _validate_queue_size(cls, v: int) -> int:
        return max(1, v)

    @field_validator("flush_interval", "flag_poll_interval", "request_timeout")
    @classmethod
    def _validate_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("interval/timeout values must be positive")
        return v
