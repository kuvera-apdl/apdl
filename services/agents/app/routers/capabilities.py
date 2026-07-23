"""Authenticated project execution-capability discovery."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth import require_project
from app.framework.gating import autonomous_mutations_enabled
from app.readiness import CodegenChangesetCapability, codegen_changeset_capability

router = APIRouter(prefix="/v1/agents/capabilities", tags=["agents"])


class ProjectExecutionCapabilities(BaseModel):
    """Effective operator policy and downstream authority for one project."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agents_project_execution_capabilities@1"] = (
        "agents_project_execution_capabilities@1"
    )
    project_id: str = Field(pattern=r"^[A-Za-z0-9]{1,64}$")
    autonomous_mutations_operator_enabled: bool
    codegen_changeset_creation: CodegenChangesetCapability


@router.get("/execution", response_model=ProjectExecutionCapabilities)
async def execution_capabilities(
    request: Request,
    project_id: str = Query(..., pattern=r"^[A-Za-z0-9]{1,64}$"),
) -> ProjectExecutionCapabilities:
    """Return fail-closed execution policy bound to the caller's project."""
    require_project(request, project_id, "agents:run")
    return ProjectExecutionCapabilities(
        project_id=project_id,
        autonomous_mutations_operator_enabled=autonomous_mutations_enabled(),
        codegen_changeset_creation=await codegen_changeset_capability(project_id),
    )
