"""Workload and control-plane authentication for the private vault."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, Request, status

from app.contracts import Consumer


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not raw.startswith(prefix) or len(raw) == len(prefix):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Vault authentication is required",
        )
    return raw[len(prefix) :]


@dataclass(frozen=True)
class AdminPrincipal:
    actor_user_id: UUID
    project_id: str


def require_admin(request: Request, project_id: str) -> AdminPrincipal:
    settings = request.app.state.settings
    if not secrets.compare_digest(_bearer(request), settings.admin_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Vault authentication failed",
        )
    asserted_project = request.headers.get("x-apdl-project-id", "")
    if not secrets.compare_digest(asserted_project, project_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project mismatch",
        )
    try:
        actor = UUID(request.headers.get("x-apdl-actor-user-id", ""))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Human actor identity is required",
        ) from exc
    return AdminPrincipal(actor_user_id=actor, project_id=project_id)


def require_consumer(request: Request, consumer: Consumer) -> None:
    settings = request.app.state.settings
    expected = (
        settings.agents_token if consumer == "agents" else settings.codegen_token
    )
    if not secrets.compare_digest(_bearer(request), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Vault workload authentication failed",
        )
