"""Repo connection model — binds an APDL project to a GitHub App installation.

Multi-tenant: each customer installs the APDL GitHub App on their repositories
and APDL records the resulting ``installation_id`` against the project. Tokens
are minted per job from this binding (see ``app/github/app_auth.py``); no
long-lived credential is stored here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConnectionCreate(BaseModel):
    """Request body for ``POST /v1/connections``."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    installation_id: int = Field(ge=1)
    #: ``owner/name`` slug of the target repository.
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    default_base_branch: str = "main"
    policy: dict[str, Any] = Field(default_factory=dict)


class Connection(BaseModel):
    """Canonical connection record as returned by the API (no secrets)."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    installation_id: int
    repo: str
    default_base_branch: str
    policy: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
