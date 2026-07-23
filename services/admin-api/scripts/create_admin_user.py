"""Provision or update an admin user without putting the password in argv."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import sys
import uuid

import asyncpg

from app.security import hash_password

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PROJECT_PATTERN = re.compile(r"^[A-Za-z0-9]{1,64}$")
ROLES = frozenset(
    {
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
    }
)
EXECUTION_ROLES = frozenset({"agents:run", "agents:manage", "agents:approve"})
MAINTENANCE_INHIBITOR_LOCK_ID = 4_158_044_083
MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--roles", nargs="+", required=True)
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read one password line from stdin instead of a TTY prompt",
    )
    parser.add_argument(
        "--allow-self-registered-execution",
        action="store_true",
        help=(
            "permanently authorize a self-registered project for Agents and "
            "Codegen execution"
        ),
    )
    parser.add_argument(
        "--override-actor",
        help="operator identity recorded with a self-registered execution override",
    )
    parser.add_argument(
        "--override-reason",
        help="single-line justification recorded with the execution override",
    )
    return parser.parse_args()


def _validated_evidence(value: str | None, *, name: str, maximum: int) -> str:
    normalized = value.strip() if value is not None else ""
    if not normalized:
        raise SystemExit(f"{name} is required")
    if len(normalized) > maximum or "\n" in normalized or "\r" in normalized:
        raise SystemExit(f"{name} must be a single line of at most {maximum} characters")
    return normalized


async def provision(args: argparse.Namespace) -> None:
    email = args.email.strip().lower()
    if EMAIL_PATTERN.fullmatch(email) is None:
        raise SystemExit("Invalid email")
    if PROJECT_PATTERN.fullmatch(args.project_id) is None:
        raise SystemExit("Invalid project ID")
    roles = sorted(set(args.roles))
    if not roles:
        raise SystemExit("At least one role is required")
    unknown = sorted(set(roles) - ROLES)
    if unknown:
        raise SystemExit(f"Unknown roles: {', '.join(unknown)}")
    requested_execution = bool(EXECUTION_ROLES.intersection(roles))
    allow_override = bool(
        getattr(args, "allow_self_registered_execution", False)
    )
    override_actor = getattr(args, "override_actor", None)
    override_reason = getattr(args, "override_reason", None)
    if allow_override and not requested_execution:
        raise SystemExit(
            "--allow-self-registered-execution requires an Agents execution role"
        )
    if not allow_override and (override_actor is not None or override_reason is not None):
        raise SystemExit(
            "--override-actor and --override-reason require "
            "--allow-self-registered-execution"
        )
    if allow_override:
        override_actor = _validated_evidence(
            override_actor,
            name="--override-actor",
            maximum=512,
        )
        override_reason = _validated_evidence(
            override_reason,
            name="--override-reason",
            maximum=2000,
        )
    dsn = os.getenv("POSTGRES_URL", "").strip()
    if not dsn:
        raise SystemExit("POSTGRES_URL is required")
    password = (
        sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass()
    )
    password_hash = hash_password(password)
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
            await conn.execute(
                """
                INSERT INTO admin_projects (project_id)
                VALUES ($1)
                ON CONFLICT (project_id) DO NOTHING
                """,
                args.project_id,
            )
            project = await conn.fetchrow(
                """
                SELECT project.created_by,
                       (execution_authority.project_id IS NOT NULL)
                           AS execution_authorized
                FROM admin_projects AS project
                LEFT JOIN admin_project_execution_authorizations
                    AS execution_authority
                  ON execution_authority.project_id = project.project_id
                WHERE project.project_id = $1
                FOR UPDATE OF project
                """,
                args.project_id,
            )
            if project is None:
                raise RuntimeError("Project provisioning did not return a project")

            self_registered = project["created_by"] is not None
            execution_authorized = bool(project["execution_authorized"])
            needs_override = (
                requested_execution
                and self_registered
                and not execution_authorized
            )
            if needs_override and not allow_override:
                raise SystemExit(
                    "Self-registered projects require "
                    "--allow-self-registered-execution with an operator actor "
                    "and reason before execution roles can be granted"
                )
            if allow_override and not needs_override:
                raise SystemExit(
                    "Execution override was requested, but this project is "
                    "already authorized or operator-provisioned"
                )
            if needs_override:
                await conn.execute(
                    """
                    INSERT INTO admin_project_execution_authorizations (
                        project_id,
                        authorization_source,
                        actor,
                        reason
                    )
                    VALUES ($1, 'self_registered_override', $2, $3)
                    """,
                    args.project_id,
                    override_actor,
                    override_reason,
                )

            user_id = await conn.fetchval(
                "SELECT user_id FROM admin_users WHERE email = $1", email
            )
            if user_id is None:
                user_id = uuid.uuid4()
                await conn.execute(
                    """
                    INSERT INTO admin_users (user_id, email, password_hash)
                    VALUES ($1, $2, $3)
                    """,
                    user_id,
                    email,
                    password_hash,
                )
            else:
                await conn.execute(
                    """
                    UPDATE admin_users
                    SET password_hash = $2, active = TRUE,
                        updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    user_id,
                    password_hash,
                )
                await conn.execute(
                    "UPDATE admin_sessions SET revoked_at = NOW() WHERE user_id = $1 AND revoked_at IS NULL",
                    user_id,
                )
            await conn.execute(
                """
                INSERT INTO admin_user_projects (user_id, project_id, roles)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, project_id) DO UPDATE SET roles = EXCLUDED.roles
                """,
                user_id,
                args.project_id,
                roles,
            )
    finally:
        await conn.close()
    print(f"Provisioned {email} for project {args.project_id}")


if __name__ == "__main__":
    asyncio.run(provision(parse_args()))
