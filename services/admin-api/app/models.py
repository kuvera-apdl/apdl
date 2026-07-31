"""Canonical admin authentication contracts."""

from datetime import datetime
import re
from typing import Annotated, Literal
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
SecurityNotificationKind = Literal["suspicious_login_activity"]
SecurityNotificationStatus = Literal["unread", "acknowledged"]
HumanRole = Literal[
    "events:write",
    "config:read",
    "config:write",
    "config:evaluate",
    "query:read",
    "agents:read",
    "agents:run",
    "agents:manage",
    "agents:approve",
    "credentials:manage",
    "members:manage",
]
MembershipAuditAction = Literal[
    "invitation_create",
    "invitation_revoke",
    "invitation_accept",
    "roles_replace",
    "member_remove",
]
ExecutionAuthorizationSource = Literal[
    "operator_provisioned", "self_registered_override"
]

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
HUMAN_ROLE_ORDER: tuple[HumanRole, ...] = (
    "events:write",
    "config:read",
    "config:write",
    "config:evaluate",
    "query:read",
    "agents:read",
    "agents:run",
    "agents:manage",
    "agents:approve",
    "credentials:manage",
    "members:manage",
)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    password: str = Field(min_length=12, max_length=1024)


class AuthCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registration_enabled: bool


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


class ProjectCreator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)


class HumanProjectOwnership(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["human"] = "human"
    owner_user_id: UUID
    owner_email: str = Field(pattern=EMAIL_PATTERN, max_length=320)


class OperatorManagedProjectOwnership(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["operator_managed"] = "operator_managed"


ProjectOwnership = Annotated[
    HumanProjectOwnership | OperatorManagedProjectOwnership,
    Field(discriminator="kind"),
]


class ExecutionAuthorizationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authorized: bool
    source: ExecutionAuthorizationSource | None

    @model_validator(mode="after")
    def validate_authorization_source(self) -> "ExecutionAuthorizationSummary":
        if self.authorized != (self.source is not None):
            raise ValueError("source must be present exactly when execution is authorized")
        return self


class ProjectAuthorizationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    creator: ProjectCreator | None
    ownership: ProjectOwnership
    execution_authorization: ExecutionAuthorizationSummary


class OwnershipTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_user_id: UUID


class OwnershipAuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: UUID
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    previous_owner_user_id: UUID | None
    previous_owner_email: str | None = Field(
        default=None,
        pattern=EMAIL_PATTERN,
        max_length=320,
    )
    new_owner_user_id: UUID
    new_owner_email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    actor: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=2000)
    created_at: datetime

    @model_validator(mode="after")
    def validate_previous_owner(self) -> "OwnershipAuditEntry":
        if (self.previous_owner_user_id is None) != (
            self.previous_owner_email is None
        ):
            raise ValueError("previous owner ID and email must be present together")
        return self


def _roles_are_canonical(roles: list[HumanRole]) -> bool:
    selected = set(roles)
    return roles == [role for role in HUMAN_ROLE_ORDER if role in selected]


class InvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    roles: list[HumanRole] = Field(min_length=1, max_length=11)

    @model_validator(mode="after")
    def validate_roles(self) -> "InvitationCreateRequest":
        if not _roles_are_canonical(self.roles):
            raise ValueError("roles must be unique and use canonical order")
        return self


class MemberRolesReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: list[HumanRole] = Field(min_length=1, max_length=11)

    @model_validator(mode="after")
    def validate_roles(self) -> "MemberRolesReplaceRequest":
        if not _roles_are_canonical(self.roles):
            raise ValueError("roles must be unique and use canonical order")
        return self


class InvitationRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=12, max_length=1024)


class ProjectMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    roles: list[HumanRole] = Field(min_length=1, max_length=11)
    active: bool
    is_owner: bool
    joined_at: datetime

    @model_validator(mode="after")
    def validate_roles(self) -> "ProjectMember":
        if not _roles_are_canonical(self.roles):
            raise ValueError("roles must be unique and use canonical order")
        return self


class PendingProjectInvitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invitation_id: UUID
    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    roles: list[HumanRole] = Field(min_length=1, max_length=11)
    inviter_email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    expires_at: datetime
    created_at: datetime

    @model_validator(mode="after")
    def validate_roles(self) -> "PendingProjectInvitation":
        if not _roles_are_canonical(self.roles):
            raise ValueError("roles must be unique and use canonical order")
        return self


class ProjectInvitationReveal(PendingProjectInvitation):
    invitation_url: str = Field(min_length=1, max_length=2048)


class ProjectMembers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    members: list[ProjectMember]
    pending_invitations: list[PendingProjectInvitation]


class InvitationInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["valid"] = "valid"
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    roles: list[HumanRole] = Field(min_length=1, max_length=11)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_roles(self) -> "InvitationInspection":
        if not _roles_are_canonical(self.roles):
            raise ValueError("roles must be unique and use canonical order")
        return self


class MembershipAuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: UUID
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    action: MembershipAuditAction
    actor_user_id: UUID
    actor_email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    subject_user_id: UUID | None
    subject_email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    invitation_id: UUID | None
    previous_roles: list[HumanRole] | None
    new_roles: list[HumanRole] | None
    created_at: datetime

    @model_validator(mode="after")
    def validate_role_snapshots(self) -> "MembershipAuditEntry":
        for roles in (self.previous_roles, self.new_roles):
            if roles is not None and not _roles_are_canonical(roles):
                raise ValueError("role snapshots must use canonical order")
        return self


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


class SecurityNotification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notification_id: UUID
    kind: SecurityNotificationKind
    status: SecurityNotificationStatus
    observed_failures: int = Field(gt=0)
    window_started_at: datetime
    last_detected_at: datetime
    created_at: datetime
