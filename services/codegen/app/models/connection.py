"""Verified GitHub repository grants and project connection models.

A project credential authorizes APDL operations for that project; it does not
prove ownership of an arbitrary repository visible to the shared GitHub App.
Repository coordinates therefore originate only from a separately verified
grant.  Public connection responses deliberately omit the installation id.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from app.safety.policy import TenantCodegenConnectionPolicy

_GRANT_ID_PATTERN = r"^ghg_[A-Za-z0-9_-]+$"
_PROJECT_ID_PATTERN = r"^[A-Za-z0-9]{1,64}$"
_REPOSITORY_NAME_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"


class RepositoryGrantStatus(StrEnum):
    """Lifecycle of independently established repository authority."""

    pending_reauthorization = "pending_reauthorization"
    active = "active"
    revoked = "revoked"


class RepositoryAuthorizationSource(StrEnum):
    """Mechanism that supplied the repository-ownership evidence."""

    github_oauth = "github_oauth"
    operator = "operator"


class RepositoryTarget(BaseModel):
    """Internal immutable coordinates used to authorize one GitHub operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str = Field(min_length=5, max_length=132, pattern=_GRANT_ID_PATTERN)
    project_id: str = Field(pattern=_PROJECT_ID_PATTERN)
    installation_id: int = Field(ge=1)
    repository_id: int = Field(ge=1)
    repository_full_name: str = Field(
        min_length=3,
        max_length=201,
        pattern=_REPOSITORY_NAME_PATTERN,
    )


class RepositoryGrant(BaseModel):
    """Canonical, auditable proof that a project may target a repository."""

    model_config = ConfigDict(extra="forbid")

    grant_id: str = Field(min_length=5, max_length=132, pattern=_GRANT_ID_PATTERN)
    project_id: str = Field(pattern=_PROJECT_ID_PATTERN)
    installation_id: int = Field(ge=1)
    repository_id: int = Field(ge=1)
    repository_full_name: str = Field(
        min_length=3,
        max_length=201,
        pattern=_REPOSITORY_NAME_PATTERN,
    )
    status: RepositoryGrantStatus
    authorization_source: RepositoryAuthorizationSource
    authorization_subject: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^[^\r\n]*\S[^\r\n]*$",
    )
    verified_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        """Keep the domain contract as strict as the database constraint."""
        if self.authorization_subject != self.authorization_subject.strip():
            raise ValueError("Authorization subject must be canonical")
        if self.status is RepositoryGrantStatus.pending_reauthorization:
            valid = self.verified_at is None and self.revoked_at is None
        elif self.status is RepositoryGrantStatus.active:
            valid = self.verified_at is not None and self.revoked_at is None
        else:
            valid = self.revoked_at is not None
        if not valid:
            raise ValueError("Repository grant timestamps do not match its status")
        return self

    @property
    def target(self) -> RepositoryTarget:
        """Return immutable internal coordinates for token authorization."""
        return RepositoryTarget(
            grant_id=self.grant_id,
            project_id=self.project_id,
            installation_id=self.installation_id,
            repository_id=self.repository_id,
            repository_full_name=self.repository_full_name,
        )


class ConnectionCreate(BaseModel):
    """Bind a project only to an already verified same-project grant.

    Repository slugs, numeric repository ids, and installation ids are not
    accepted here.  Grant creation is an operator / verified OAuth operation,
    separate from the tenant connection API.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=_PROJECT_ID_PATTERN)
    grant_id: str = Field(min_length=5, max_length=132, pattern=_GRANT_ID_PATTERN)
    default_base_branch: str = Field(
        default="main",
        min_length=1,
        max_length=255,
        pattern=r"^[^\r\n]+$",
    )


class Connection(BaseModel):
    """Public connection record plus a serialization-excluded trusted target."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=_PROJECT_ID_PATTERN)
    grant_id: str = Field(min_length=5, max_length=132, pattern=_GRANT_ID_PATTERN)
    repository_id: int = Field(ge=1)
    repository_full_name: str = Field(
        min_length=3,
        max_length=201,
        pattern=_REPOSITORY_NAME_PATTERN,
    )
    default_base_branch: str
    tenant_policy: TenantCodegenConnectionPolicy
    created_at: datetime
    updated_at: datetime

    _target: RepositoryTarget | None = PrivateAttr(default=None)

    @property
    def target(self) -> RepositoryTarget:
        """Internal grant coordinates; never included in an API response."""
        if self._target is None:
            raise RuntimeError("Connection repository target was not loaded")
        return self._target

    def attach_target(self, target: RepositoryTarget) -> Connection:
        """Attach store-validated internal coordinates and return ``self``."""
        if (
            target.project_id != self.project_id
            or target.grant_id != self.grant_id
            or target.repository_id != self.repository_id
            or target.repository_full_name != self.repository_full_name
        ):
            raise ValueError("Connection target does not match its public identity")
        self._target = target
        return self
