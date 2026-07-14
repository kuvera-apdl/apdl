"""Shared fixtures and builders for the ETL test suite."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from etl import EtlContext

RECEIVED_AT = datetime(2026, 5, 26, 10, 11, 12, tzinfo=timezone.utc)


@pytest.fixture
def ctx() -> EtlContext:
    return EtlContext(
        project_id="project42",
        received_at=RECEIVED_AT,
        ingested_at=RECEIVED_AT,
        ip="203.0.113.7",
        source="sdk-js@2.4.1",
    )


def make_envelope(schema: str, payload: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Build a minimal canonical-envelope dict for a given schema."""
    env = {
        "_id": str(uuid4()),
        "_schema": schema,
        "_project_id": "project42",
        "_idempotency_key": overrides.pop("idempotency_key", "msg-1"),
        "_source": "sdk-js@2.4.1",
        "_occurred_at": "2026-05-26T10:11:12.000Z",
        "payload": payload,
    }
    env.update(overrides)
    return env
