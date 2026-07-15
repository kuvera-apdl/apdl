"""GitHub App introspection endpoints.

Read-only views of what the App can reach on github.com, used by the admin
console to turn "connect a repo" into a picker instead of hand-typed slugs and
installation ids. Guarded by the internal token like the connection registry.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.auth import require_internal_token
from app.github.installations import AccessibleRepo, list_accessible_repos

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/github",
    tags=["github"],
    dependencies=[Depends(require_internal_token)],
)


@router.get("/repos", response_model=list[AccessibleRepo])
async def get_accessible_repos() -> list[AccessibleRepo]:
    """Every repository the App is installed on, across all installations.

    An empty list is a valid answer (the App has no installations yet) — the
    console renders it as "install the App first" guidance, not an error.
    """
    try:
        return await list_accessible_repos()
    except ValueError as exc:
        # build_app_jwt: App ID / private key not configured on this deployment.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        logger.warning("GitHub repo listing failed: %s", exc)
        raise HTTPException(
            status_code=502, detail=f"GitHub repository listing failed: {exc}"
        ) from exc
