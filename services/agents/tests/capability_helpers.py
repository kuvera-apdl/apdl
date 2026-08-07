from __future__ import annotations

from typing import Any

from app.service_auth import ServiceCapabilityContext


def make_service_capability(
    *,
    project_id: str = "demo",
    execution_kind: str = "agent_run",
    execution_id: str = "run-1",
    run_id: str = "run-1",
    execution_owner_id: str = "lease-owner-1",
    pool: Any | None = None,
) -> ServiceCapabilityContext:
    """Build an explicit execution-bound capability context for unit tests."""
    return ServiceCapabilityContext(
        pool=pool or object(),
        project_id=project_id,
        execution_kind=execution_kind,
        execution_id=execution_id,
        run_id=run_id,
        execution_owner_id=execution_owner_id,
    )


def make_mutation_capability(
    *,
    project_id: str = "demo",
    run_id: str = "run-1",
    pool: Any | None = None,
) -> ServiceCapabilityContext:
    """Build a leased approval-effect context for mutation tool tests."""
    return make_service_capability(
        project_id=project_id,
        execution_kind="approval_effect",
        execution_id="4b42b08a-52fe-4d73-8ed8-a5d9a67a2f21",
        run_id=run_id,
        execution_owner_id="effect-owner-1",
        pool=pool,
    )
