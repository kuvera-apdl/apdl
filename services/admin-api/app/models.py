"""Canonical admin authentication contracts."""

from pydantic import BaseModel, ConfigDict, Field

EMAIL_PATTERN = r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
PROJECT_ID_PATTERN = r"^[A-Za-z0-9]{1,64}$"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    password: str = Field(min_length=12, max_length=1024)


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=PROJECT_ID_PATTERN)


class ProjectAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    roles: list[str]


class UserIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    projects: list[ProjectAccess]
