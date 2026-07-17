"""Client configuration model with strict, self-hosted-safe defaults."""

from __future__ import annotations

import re
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
)

# Canonical API-key wire format. The project segment is a client-side hint;
# services derive tenant authority only from the verified database record.
_KEY_PATTERN = re.compile(r"^proj_([a-zA-Z0-9]{1,64})_([a-zA-Z0-9]{16,128})$")

DEFAULT_BATCH_SIZE = 20
MAX_BATCH_SIZE = 100
DEFAULT_FLUSH_INTERVAL = 3.0
DEFAULT_MAX_QUEUE_SIZE = 1000
DEFAULT_FLAG_POLL_INTERVAL = 30.0
DEFAULT_REQUEST_TIMEOUT = 10.0

BatchSize = Annotated[StrictInt, Field(ge=1, le=MAX_BATCH_SIZE)]
QueueSize = Annotated[StrictInt, Field(ge=1)]
PositiveSeconds = Annotated[StrictFloat, Field(gt=0)]


class APDLConfig(BaseModel):
    """Resolved, validated SDK configuration.

    Mirrors the JS SDK's ``APDLConfig``/``resolveConfig`` where it makes sense
    for a server-side client. Browser-only concepts (persistence, consent,
    auto-capture, UI) are intentionally omitted.

    A single ``endpoint`` origin serves both event ingestion (``/v1/events``)
    and flag config (``/v1/flags``); a gateway routes each path to the right
    service behind that origin.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    api_key: StrictStr
    endpoint: StrictStr

    # Event batching
    batch_size: BatchSize = DEFAULT_BATCH_SIZE
    flush_interval: PositiveSeconds = DEFAULT_FLUSH_INTERVAL
    max_queue_size: QueueSize = DEFAULT_MAX_QUEUE_SIZE

    # Feature flags
    enable_flags: StrictBool = True
    flag_poll_interval: PositiveSeconds = DEFAULT_FLAG_POLL_INTERVAL
    log_exposures: StrictBool = True

    # Transport
    request_timeout: PositiveSeconds = DEFAULT_REQUEST_TIMEOUT
    debug: StrictBool = False

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("APDL: api_key is required and must be a non-empty string")
        if not _KEY_PATTERN.match(v):
            raise ValueError(
                "APDL: api_key must match format proj_{project_id}_{secret} "
                "(secret: 16+ alphanumeric characters)"
            )
        return v

    @property
    def project_id(self) -> str:
        """Return the project hint used for local SDK project-scoped state."""
        match = _KEY_PATTERN.match(self.api_key)
        assert match is not None  # guaranteed by _validate_api_key
        return match.group(1)

    @field_validator("endpoint")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        if not isinstance(v, str) or v != v.strip() or not v:
            raise ValueError("APDL: endpoint is required and must be an HTTP(S) origin")
        parsed = urlsplit(v)
        try:
            hostname = parsed.hostname
            parsed.port
        except ValueError as exc:
            raise ValueError("APDL: endpoint contains an invalid port") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "APDL: endpoint must be an HTTP(S) origin without credentials, path, "
                "query, or fragment"
            )
        return v.rstrip("/")
