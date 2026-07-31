"""Assign an eligible project owner through an audited operator workflow."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import uuid

import asyncpg

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PROJECT_PATTERN = re.compile(r"^[A-Za-z0-9]{1,64}$")
MAINTENANCE_INHIBITOR_LOCK_ID = 4_158_044_083
MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--owner-email", required=True)
    parser.add_argument(
        "--actor",
        required=True,
        help="operator identity recorded in the immutable ownership audit",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="single-line recovery or provisioning justification",
    )
    return parser.parse_args()


def _evidence(value: str, *, name: str, maximum: int) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or "\n" in normalized
        or "\r" in normalized
    ):
        raise SystemExit(f"{name} must be a single line of at most {maximum} characters")
    return normalized


async def assign_owner(args: argparse.Namespace) -> None:
    project_id = args.project_id.strip()
    email = args.owner_email.strip().lower()
    actor = _evidence(args.actor, name="--actor", maximum=512)
    reason = _evidence(args.reason, name="--reason", maximum=2000)
    if PROJECT_PATTERN.fullmatch(project_id) is None:
        raise SystemExit("Invalid project ID")
    if EMAIL_PATTERN.fullmatch(email) is None:
        raise SystemExit("Invalid owner email")

    dsn = os.getenv("POSTGRES_URL", "").strip()
    if not dsn:
        raise SystemExit("POSTGRES_URL is required")

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "SELECT pg_advisory_lock_shared($1)",
            MAINTENANCE_INHIBITOR_LOCK_ID,
        )
        await conn.execute(
            "SELECT pg_advisory_lock_shared($1)",
            MAINTENANCE_GUARD_LOCK_ID,
        )
        async with conn.transaction():
            project = await conn.fetchrow(
                """
                SELECT owner_user_id
                FROM admin_projects
                WHERE project_id = $1
                FOR UPDATE
                """,
                project_id,
            )
            if project is None:
                raise SystemExit("Project does not exist")

            target = await conn.fetchrow(
                """
                SELECT account.user_id, account.active, membership.roles
                FROM admin_users AS account
                JOIN admin_user_projects AS membership
                  ON membership.user_id = account.user_id
                 AND membership.project_id = $1
                WHERE account.email = $2
                FOR UPDATE OF account, membership
                """,
                project_id,
                email,
            )
            if (
                target is None
                or not target["active"]
                or "members:manage" not in target["roles"]
            ):
                raise SystemExit(
                    "Owner must be an active project member with members:manage"
                )

            previous_owner_user_id = project["owner_user_id"]
            target_user_id = target["user_id"]
            if previous_owner_user_id == target_user_id:
                raise SystemExit("Target user already owns the project")

            await conn.execute(
                """
                UPDATE admin_projects
                SET owner_user_id = $2
                WHERE project_id = $1
                """,
                project_id,
                target_user_id,
            )
            await conn.execute(
                """
                INSERT INTO admin_project_ownership_audit (
                    audit_id,
                    project_id,
                    previous_owner_user_id,
                    new_owner_user_id,
                    actor,
                    reason
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                uuid.uuid4(),
                project_id,
                previous_owner_user_id,
                target_user_id,
                actor,
                reason,
            )
    finally:
        await conn.close()

    print(f"Assigned {email} as owner of project {project_id}")


if __name__ == "__main__":
    asyncio.run(assign_owner(parse_args()))
