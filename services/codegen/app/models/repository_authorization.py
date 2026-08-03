"""Strict public contracts for project-scoped GitHub repository onboarding."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

_PROJECT_ID_PATTERN = r"^[A-Za-z0-9]{1,64}$"
_REPOSITORY_NAME_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"


class RepositoryAuthorizationStatus(StrEnum):
    """Persisted lifecycle for one short-lived GitHub authorization flow."""

    awaiting_installation = "awaiting_installation"
    awaiting_oauth = "awaiting_oauth"
    awaiting_selection = "awaiting_selection"
    completed = "completed"


class RepositoryAuthorizationStart(BaseModel):
    """Start repository discovery for exactly one project."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=_PROJECT_ID_PATTERN)


class RepositoryAuthorizationStarted(BaseModel):
    """Opaque browser handoff for installing/authorizing the GitHub App."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["github_repository_authorization_start@1"] = (
        "github_repository_authorization_start@1"
    )
    authorization_id: UUID
    installation_url: AnyHttpUrl
    expires_at: datetime


class RepositoryAuthorizationRepository(BaseModel):
    """One server-discovered repository the GitHub user may administer."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID
    repository_id: int = Field(ge=1)
    repository_full_name: str = Field(
        min_length=3,
        max_length=201,
        pattern=_REPOSITORY_NAME_PATTERN,
    )
    default_base_branch: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[^\r\n]+$",
    )
    private: bool


class RepositoryAuthorization(BaseModel):
    """Current authorization state plus opaque repository choices."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["github_repository_authorization@1"] = (
        "github_repository_authorization@1"
    )
    authorization_id: UUID
    project_id: str = Field(pattern=_PROJECT_ID_PATTERN)
    status: RepositoryAuthorizationStatus
    expires_at: datetime
    repositories: list[RepositoryAuthorizationRepository] = Field(max_length=1_000)

    @model_validator(mode="after")
    def validate_repositories(self) -> Self:
        if (
            self.status is not RepositoryAuthorizationStatus.awaiting_selection
            and self.repositories
        ):
            raise ValueError("Repositories are visible only while awaiting selection")
        candidate_ids = [item.candidate_id for item in self.repositories]
        repository_ids = [item.repository_id for item in self.repositories]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Repository candidate ids must be unique")
        if len(repository_ids) != len(set(repository_ids)):
            raise ValueError("Repository ids must be unique")
        return self


class RepositoryAuthorizationComplete(BaseModel):
    """Bind one previously discovered opaque candidate to its project."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=_PROJECT_ID_PATTERN)
    candidate_id: UUID


class DiscoveredRepository(BaseModel):
    """Internal validated repository evidence returned by GitHub."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    installation_id: int = Field(ge=1)
    repository_id: int = Field(ge=1)
    repository_full_name: str = Field(
        min_length=3,
        max_length=201,
        pattern=_REPOSITORY_NAME_PATTERN,
    )
    default_base_branch: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[^\r\n]+$",
    )
    private: bool
