"""Canonical admin authentication contracts."""

from pydantic import BaseModel, ConfigDict, Field

EMAIL_PATTERN = r"^[^\s@]+@[^\s@]+\.[^\s@]+$"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class ProjectAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    roles: list[str]


class UserIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    projects: list[ProjectAccess]
