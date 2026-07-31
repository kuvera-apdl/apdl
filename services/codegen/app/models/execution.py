"""Canonical serving execution and publication primitives."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
DockerImageId = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class RiskLevel(str, Enum):
    """Server-owned risk classification for one Codegen changeset."""

    low = "low"
    medium = "medium"
    high = "high"


class PublicationStage(str, Enum):
    """Serving stages with explicit GitHub publication semantics."""

    offline = "offline"
    development_pr = "development_pr"
    tenant_draft_pr = "tenant_draft_pr"


def canonical_sha256(value: BaseModel | dict | list) -> str:
    """Hash a strict schema artifact with stable JSON encoding."""
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
