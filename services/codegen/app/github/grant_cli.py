"""Authorize one GitHub repository for a project as a trusted local operator.

This command is intentionally packaged separately from the FastAPI application.
Repository discovery uses the GitHub App only to resolve immutable repository
identity; the operator invocation is the authorization boundary that records
the resulting project grant.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re

import asyncpg

from app.config import postgres_url
from app.db import assert_schema_ready
from app.github.app_auth import resolve_repository_target
from app.models.connection import Connection
from app.store.connections import (
    activate_operator_grant,
    revoke_repository_grant as revoke_stored_repository_grant,
)

_PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,64}$")
_GRANT_ID_PATTERN = re.compile(r"^ghg_[A-Za-z0-9_-]{1,128}$")


def _project_id(value: str) -> str:
    if _PROJECT_ID_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "project id must contain 1-64 ASCII letters or digits"
        )
    return value


def _authorization_subject(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise argparse.ArgumentTypeError("authorized-by must not be blank")
    if len(normalized) > 512:
        raise argparse.ArgumentTypeError("authorized-by must be at most 512 characters")
    if "\r" in normalized or "\n" in normalized:
        raise argparse.ArgumentTypeError("authorized-by must be a single line")
    return normalized


def _grant_id(value: str) -> str:
    if _GRANT_ID_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("grant id must use the canonical ghg_ format")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-id",
        required=True,
        type=_project_id,
        help="APDL project receiving the repository grant",
    )
    parser.add_argument(
        "--repository",
        required=True,
        metavar="OWNER/NAME",
        help="repository to discover through the configured GitHub App",
    )
    parser.add_argument(
        "--authorized-by",
        required=True,
        type=_authorization_subject,
        help="operator identity or change-ticket reference recorded for audit",
    )
    return parser


def _revoke_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Irreversibly revoke one active Codegen repository grant."
    )
    parser.add_argument(
        "--project-id",
        required=True,
        type=_project_id,
        help="APDL project that owns the active repository grant",
    )
    parser.add_argument(
        "--grant-id",
        required=True,
        type=_grant_id,
        help="exact active grant identifier returned by the grant command",
    )
    return parser


async def activate_repository_grant(
    *,
    project_id: str,
    repository: str,
    authorized_by: str,
) -> Connection:
    """Validate schema, discover the repository, and atomically activate it."""
    pool = await asyncpg.create_pool(
        dsn=postgres_url(),
        min_size=1,
        max_size=2,
    )
    try:
        async with pool.acquire() as conn:
            await assert_schema_ready(conn)

        discovered = await resolve_repository_target(repository)
        return await activate_operator_grant(
            pool,
            project_id=project_id,
            installation_id=discovered.installation_id,
            repository_id=discovered.repository_id,
            repository_full_name=discovered.repository_full_name,
            default_base_branch=discovered.default_branch,
            authorization_subject=authorized_by,
        )
    finally:
        await pool.close()


async def revoke_repository_grant(*, project_id: str, grant_id: str) -> None:
    """Validate schema and irreversibly revoke one exact same-project grant."""
    pool = await asyncpg.create_pool(
        dsn=postgres_url(),
        min_size=1,
        max_size=2,
    )
    try:
        async with pool.acquire() as conn:
            await assert_schema_ready(conn)
        revoked = await revoke_stored_repository_grant(
            pool,
            project_id=project_id,
            grant_id=grant_id,
        )
        if not revoked:
            raise RuntimeError(
                "Active repository grant was not found for the specified project"
            )
    finally:
        await pool.close()


def main() -> None:
    """Run the trusted operator grant command."""
    args = _parser().parse_args()
    connection = asyncio.run(
        activate_repository_grant(
            project_id=args.project_id,
            repository=args.repository,
            authorized_by=args.authorized_by,
        )
    )
    print(
        json.dumps(
            {
                "grant_id": connection.grant_id,
                "project_id": connection.project_id,
                "repository_full_name": connection.repository_full_name,
                "repository_id": connection.repository_id,
                "status": "active",
            },
            indent=2,
            sort_keys=True,
        )
    )


def revoke_main() -> None:
    """Run the trusted operator revocation command."""
    args = _revoke_parser().parse_args()
    asyncio.run(
        revoke_repository_grant(
            project_id=args.project_id,
            grant_id=args.grant_id,
        )
    )
    print(
        json.dumps(
            {
                "grant_id": args.grant_id,
                "project_id": args.project_id,
                "status": "revoked",
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
