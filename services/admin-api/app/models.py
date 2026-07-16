"""Canonical admin authentication contracts."""

from datetime import datetime
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

EMAIL_PATTERN = r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
PROJECT_ID_PATTERN = r"^[A-Za-z0-9]{1,64}$"
MANAGED_CREDENTIAL_ID_PATTERN = r"^managed-[0-9a-f]{32}$"

CredentialKind = Literal["browser", "confidential"]
CredentialRole = Literal[
    "events:write",
    "config:read",
    "config:evaluate",
    "query:read",
]
CredentialAuditAction = Literal["create", "rotate", "revoke"]

MANAGED_CREDENTIAL_ROLE_ORDER: tuple[CredentialRole, ...] = (
    "events:write",
    "config:read",
    "config:evaluate",
    "query:read",
)
BROWSER_CREDENTIAL_ROLES: tuple[CredentialRole, ...] = (
    "events:write",
    "config:read",
)


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


class CredentialCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_kind: CredentialKind
    roles: list[CredentialRole] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_canonical_roles(self) -> "CredentialCreateRequest":
        if self.credential_kind == "browser":
            expected = list(BROWSER_CREDENTIAL_ROLES)
        else:
            selected = set(self.roles)
            expected = [
                role for role in MANAGED_CREDENTIAL_ROLE_ORDER if role in selected
            ]
        if self.roles != expected:
            raise ValueError(
                "roles must be unique and use canonical least-privilege order"
            )
        return self


class CredentialActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManagedCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_id: str = Field(pattern=MANAGED_CREDENTIAL_ID_PATTERN)
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    credential_kind: CredentialKind
    key_prefix: str = Field(min_length=1, max_length=72)
    roles: list[CredentialRole] = Field(min_length=1, max_length=4)
    active: bool
    created_at: datetime
    revoked_at: datetime | None
    rotated_from_credential_id: str | None = Field(
        default=None, pattern=MANAGED_CREDENTIAL_ID_PATTERN
    )

    @model_validator(mode="after")
    def validate_metadata_contract(self) -> "ManagedCredential":
        expected_prefix = (
            f"client_{self.project_id}_"
            if self.credential_kind == "browser"
            else f"proj_{self.project_id}_"
        )
        selected = set(self.roles)
        expected_roles = [
            role for role in MANAGED_CREDENTIAL_ROLE_ORDER if role in selected
        ]
        if self.key_prefix != expected_prefix:
            raise ValueError("key_prefix does not match credential kind and project")
        if self.roles != expected_roles:
            raise ValueError("roles must be unique and use canonical order")
        if (
            self.credential_kind == "browser"
            and self.roles != list(BROWSER_CREDENTIAL_ROLES)
        ):
            raise ValueError("browser credentials require the exact browser roles")
        if self.active == (self.revoked_at is not None):
            raise ValueError("active and revoked_at must describe one lifecycle state")
        return self


class ManagedCredentialReveal(ManagedCredential):
    api_key: str = Field(min_length=32, max_length=256)

    @model_validator(mode="after")
    def validate_revealed_key(self) -> "ManagedCredentialReveal":
        secret = self.api_key.removeprefix(self.key_prefix)
        if (
            not self.api_key.startswith(self.key_prefix)
            or re.fullmatch(r"[A-Za-z0-9]{16,128}", secret) is None
        ):
            raise ValueError("api_key does not match key_prefix")
        return self


class CredentialAuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: UUID
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    credential_id: str = Field(pattern=MANAGED_CREDENTIAL_ID_PATTERN)
    action: CredentialAuditAction
    actor_user_id: UUID
    actor_email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    credential_kind: CredentialKind
    roles: list[CredentialRole] = Field(min_length=1, max_length=4)
    successor_credential_id: str | None = Field(
        default=None, pattern=MANAGED_CREDENTIAL_ID_PATTERN
    )
    created_at: datetime

    @model_validator(mode="after")
    def validate_audit_contract(self) -> "CredentialAuditEntry":
        selected = set(self.roles)
        expected_roles = [
            role for role in MANAGED_CREDENTIAL_ROLE_ORDER if role in selected
        ]
        if self.roles != expected_roles:
            raise ValueError("roles must be unique and use canonical order")
        if (
            self.credential_kind == "browser"
            and self.roles != list(BROWSER_CREDENTIAL_ROLES)
        ):
            raise ValueError("browser credentials require the exact browser roles")
        if (self.action == "rotate") != (self.successor_credential_id is not None):
            raise ValueError("only rotate audit entries identify a successor")
        return self
