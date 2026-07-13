"""Repo connection model — binds an APDL project to a GitHub App installation.

Multi-tenant: each customer installs the APDL GitHub App on their repositories
and APDL records the resulting ``installation_id`` against the project. Tokens
are minted per job from this binding (see ``app/github/app_auth.py``); no
long-lived credential is stored here.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.safety.policy import TenantCodegenConnectionPolicy


class ConnectionCreate(BaseModel):
    """Request body for ``POST /v1/connections``."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    #: Omit to have the service resolve the live installation id from the repo
    #: slug (the id also self-heals at token-mint time, so it is a cache hint).
    installation_id: int | None = Field(default=None, ge=1)
    #: ``owner/name`` slug of the target repository.
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    default_base_branch: str = "main"


class Connection(BaseModel):
    """Canonical connection record as returned by the API (no secrets)."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    installation_id: int
    repo: str
    default_base_branch: str
    tenant_policy: TenantCodegenConnectionPolicy
    created_at: datetime
    updated_at: datetime
