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
    }
)


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
    return parser.parse_args()


async def provision(args: argparse.Namespace) -> None:
    email = args.email.strip().lower()
    if EMAIL_PATTERN.fullmatch(email) is None:
        raise SystemExit("Invalid email")
    if PROJECT_PATTERN.fullmatch(args.project_id) is None:
        raise SystemExit("Invalid project ID")
    roles = sorted(set(args.roles))
    unknown = sorted(set(roles) - ROLES)
    if unknown:
        raise SystemExit(f"Unknown roles: {', '.join(unknown)}")
    password = (
        sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass()
    )
    password_hash = hash_password(password)
    dsn = os.getenv("POSTGRES_URL", "postgresql://apdl:apdl_dev@localhost:5432/apdl")
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
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
                        failed_login_attempts = 0, locked_until = NULL,
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
