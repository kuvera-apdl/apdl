"""Internal service-to-service authentication.

The codegen service is internal: it is called by the agents service and by
GitHub webhooks, never directly by a browser. Endpoints under ``/v1`` require
the shared APDL internal token when one is configured. In local dev (no token
set) the guard is permissive, matching the rest of the platform.
"""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from app.config import internal_token


async def require_internal_token(
    x_apdl_internal_token: str | None = Header(default=None),
) -> None:
    """FastAPI dependency enforcing the internal token when configured."""
    expected = internal_token()
    if not expected:
        # Local dev: no token configured — allow.
        return
    if x_apdl_internal_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal token.",
        )
