#!/usr/bin/env python3
"""Delete one project or user from every canonical analytics identity store."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from typing import Any

import migrate as maintenance


REQUIRED_CLICKHOUSE_MIGRATION = (16, "016_personal_data_retention.sql")
REQUIRED_POSTGRES_MIGRATION = (
    40,
    "040_analytics_data_deletion_audit.sql",
)
PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,64}$")
TARGET_TABLES = (
    "feature_flag_exposures",
    "frontend_health_events",
    "sessions",
    "experiment_event_deliveries",
    "events",
    # Keep assertions until last so an interrupted user deletion can recover
    # its anonymous aliases on retry after every other identity store changed.
    "identity_alias_assertions",
)


class DeletionError(RuntimeError):
    """The deletion request or its durable completion proof is invalid."""


@dataclass(frozen=True)
class DeletionRequest:
    request_id: str
    scope: str
    project_id: str
    user_id: str | None
    actor: str
    reason: str
    target_sha256: str
    request_sha256: str


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_text(value: str, field: str, maximum: int) -> str:
    if not value or value.strip() == "" or len(value) > maximum or "\x00" in value:
        raise DeletionError(
            f"{field} must be nonblank UTF-8 text of at most {maximum} characters"
        )
    return value


def _request_from_args(args: argparse.Namespace) -> DeletionRequest:
    try:
        parsed_request_id = uuid.UUID(args.request_id)
    except (AttributeError, ValueError) as exc:
        raise DeletionError("request_id must be a canonical UUID") from exc
    request_id = str(parsed_request_id)
    if args.request_id != request_id:
        raise DeletionError("request_id must use canonical lowercase UUID notation")

    project_id = args.project_id
    if PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise DeletionError(
            "project_id must contain 1-64 ASCII letters or digits"
        )
    actor = _bounded_text(args.actor, "actor", 512)
    reason = _bounded_text(args.reason, "reason", 2_000)
    user_id = None
    if args.scope == "user":
        user_id = _bounded_text(args.user_id, "user_id", 128)

    target = {
        "scope": args.scope,
        "project_id": project_id,
        "user_id": user_id,
    }
    request_contract = {
        **target,
        "request_id": request_id,
        "actor": actor,
        "reason": reason,
    }
    return DeletionRequest(
        request_id=request_id,
        scope=args.scope,
        project_id=project_id,
        user_id=user_id,
        actor=actor,
        reason=reason,
        target_sha256=_canonical_sha256(target),
        request_sha256=_canonical_sha256(request_contract),
    )


def _clickhouse_base64(value: str) -> str:
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"base64Decode('{encoded}')"


def _postgres_text(value: str) -> str:
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"convert_from(decode('{encoded}', 'base64'), 'UTF8')"


def _target_condition(
    request: DeletionRequest,
    anonymous_ids_base64: tuple[str, ...],
) -> str:
    project = _clickhouse_base64(request.project_id)
    if request.scope == "project":
        return f"project_id = {project}"

    assert request.user_id is not None
    identities = [f"user_id = {_clickhouse_base64(request.user_id)}"]
    if anonymous_ids_base64:
        aliases = ", ".join(
            f"base64Decode('{value}')" for value in anonymous_ids_base64
        )
        identities.append(f"anonymous_id IN ({aliases})")
    return f"project_id = {project} AND ({' OR '.join(identities)})"


def _run_psql(
    sql: str,
    fence: maintenance.MaintenanceFence,
) -> str:
    command = [
        *maintenance._maintenance_psql_command(),
        "-X",
        "-q",
        "-A",
        "-t",
        "-v",
        "ON_ERROR_STOP=1",
    ]
    environment = os.environ.copy()
    environment.setdefault("PGCONNECT_TIMEOUT", "5")
    fence.assert_held()
    result = subprocess.run(
        command,
        input=sql,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        env=environment,
    )
    fence.assert_held()
    return result.stdout


def _read_audit_events(
    request_id: str,
    fence: maintenance.MaintenanceFence,
) -> dict[str, dict[str, Any]]:
    rows = _run_psql(
        """
        SELECT replace(
            encode(convert_to(row_to_json(audit_row)::text, 'UTF8'), 'base64'),
            E'\\n',
            ''
        )
        FROM (
            SELECT
                event_type,
                scope,
                project_id,
                target_sha256,
                request_sha256,
                actor,
                reason,
                details
            FROM analytics_data_deletion_audit
            WHERE request_id = """
        + f"'{request_id}'::uuid"
        + """
            ORDER BY event_type
        ) AS audit_row;
        """,
        fence,
    )
    events: dict[str, dict[str, Any]] = {}
    for encoded in rows.splitlines():
        if not encoded:
            continue
        try:
            event = json.loads(base64.b64decode(encoded).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise DeletionError("deletion audit ledger returned invalid data") from exc
        event_type = event.get("event_type")
        if event_type not in {"requested", "completed"}:
            raise DeletionError("deletion audit ledger contains an invalid event")
        events[event_type] = event
    return events


def _assert_postgres_schema(fence: maintenance.MaintenanceFence) -> None:
    ledger_exists = _run_psql(
        "SELECT to_regclass('public.apdl_schema_migrations') IS NOT NULL;",
        fence,
    ).strip()
    if ledger_exists != "t":
        raise DeletionError("PostgreSQL migration ledger is not installed")

    version, name = REQUIRED_POSTGRES_MIGRATION
    recorded = _run_psql(
        "SELECT name FROM public.apdl_schema_migrations "
        f"WHERE version = {version};",
        fence,
    ).strip()
    if recorded != name:
        raise DeletionError(
            f"required PostgreSQL migration {name} is not applied"
        )


def _validate_audit_identity(
    request: DeletionRequest,
    events: dict[str, dict[str, Any]],
) -> None:
    for event in events.values():
        expected = {
            "scope": request.scope,
            "project_id": request.project_id,
            "target_sha256": request.target_sha256,
            "request_sha256": request.request_sha256,
            "actor": request.actor,
            "reason": request.reason,
        }
        if any(event.get(field) != value for field, value in expected.items()):
            raise DeletionError(
                f"request_id {request.request_id} is already bound to another request"
            )


def _record_audit_event(
    request: DeletionRequest,
    event_type: str,
    details: dict[str, Any],
    fence: maintenance.MaintenanceFence,
) -> None:
    if event_type not in {"requested", "completed"}:
        raise DeletionError("audit event type is not canonical")
    details_json = json.dumps(
        details,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    _run_psql(
        f"""
        INSERT INTO analytics_data_deletion_audit (
            request_id,
            event_type,
            scope,
            project_id,
            target_sha256,
            request_sha256,
            actor,
            reason,
            details
        )
        VALUES (
            '{request.request_id}'::uuid,
            '{event_type}',
            {_postgres_text(request.scope)},
            {_postgres_text(request.project_id)},
            '{request.target_sha256}',
            '{request.request_sha256}',
            {_postgres_text(request.actor)},
            {_postgres_text(request.reason)},
            {_postgres_text(details_json)}::jsonb
        )
        ON CONFLICT (request_id, event_type) DO NOTHING;
        """,
        fence,
    )


def _assert_clickhouse_schema(
    client: maintenance.ClickHouseClient,
    fence: maintenance.MaintenanceFence,
) -> None:
    version, name = REQUIRED_CLICKHOUSE_MIGRATION
    recorded = client.execute(
        "SELECT name FROM apdl_schema_migrations FINAL "
        f"WHERE version = {version}",
        fence=fence,
        capture=True,
    ).strip()
    if recorded != name:
        raise DeletionError(
            f"required ClickHouse migration {name} is not applied"
        )


def _linked_anonymous_ids(
    request: DeletionRequest,
    client: maintenance.ClickHouseClient,
    fence: maintenance.MaintenanceFence,
) -> tuple[str, ...]:
    if request.scope != "user":
        return ()
    assert request.user_id is not None
    output = client.execute(
        "SELECT DISTINCT base64Encode(anonymous_id) "
        "FROM identity_alias_assertions FINAL "
        f"WHERE project_id = {_clickhouse_base64(request.project_id)} "
        f"AND user_id = {_clickhouse_base64(request.user_id)} "
        "AND anonymous_id != '' "
        "ORDER BY base64Encode(anonymous_id)",
        fence=fence,
        capture=True,
    )
    aliases = tuple(value for value in output.splitlines() if value)
    if any(re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", value) is None for value in aliases):
        raise DeletionError("ClickHouse returned an invalid anonymous ID encoding")
    return aliases


def _delete_target_rows(
    request: DeletionRequest,
    aliases: tuple[str, ...],
    client: maintenance.ClickHouseClient,
    fence: maintenance.MaintenanceFence,
) -> dict[str, int]:
    condition = _target_condition(request, aliases)
    matched_rows: dict[str, int] = {}
    for table in TARGET_TABLES:
        raw_count = client.execute(
            f"SELECT count() FROM `{table}` WHERE {condition}",
            fence=fence,
            capture=True,
        ).strip()
        try:
            matched_rows[table] = int(raw_count)
        except ValueError as exc:
            raise DeletionError(f"could not count deletion target {table}") from exc
        if matched_rows[table] > 0:
            client.execute(
                f"ALTER TABLE `{table}` DELETE WHERE {condition} "
                "SETTINGS mutations_sync = 2",
                fence=fence,
            )
        remaining = client.execute(
            f"SELECT count() FROM `{table}` WHERE {condition}",
            fence=fence,
            capture=True,
        ).strip()
        if remaining != "0":
            raise DeletionError(f"deletion did not converge for table {table}")
    return matched_rows


def execute_request(
    request: DeletionRequest,
    client: maintenance.ClickHouseClient,
    fence: maintenance.MaintenanceFence,
) -> dict[str, Any]:
    events = _read_audit_events(request.request_id, fence)
    _validate_audit_identity(request, events)
    if "completed" in events:
        return {
            "request_id": request.request_id,
            "scope": request.scope,
            "project_id": request.project_id,
            "status": "already_completed",
            "details": events["completed"]["details"],
        }

    if "requested" not in events:
        _record_audit_event(request, "requested", {}, fence)
        events = _read_audit_events(request.request_id, fence)
        _validate_audit_identity(request, events)
        if "requested" not in events:
            raise DeletionError("deletion request was not durably recorded")

    aliases = _linked_anonymous_ids(request, client, fence)
    matched_rows = _delete_target_rows(request, aliases, client, fence)
    details = {
        "anonymous_id_count": len(aliases),
        "matched_rows": matched_rows,
    }
    _record_audit_event(request, "completed", details, fence)
    events = _read_audit_events(request.request_id, fence)
    _validate_audit_identity(request, events)
    if events.get("completed", {}).get("details") != details:
        raise DeletionError("deletion completion was not durably recorded")
    return {
        "request_id": request.request_id,
        "scope": request.scope,
        "project_id": request.project_id,
        "status": "completed",
        "details": details,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="scope", required=True)
    for scope in ("project", "user"):
        command = subparsers.add_parser(scope)
        command.add_argument("--request-id", required=True)
        command.add_argument("--project-id", required=True)
        if scope == "user":
            command.add_argument("--user-id", required=True)
        command.add_argument("--actor", required=True)
        command.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        request = _request_from_args(_parser().parse_args(argv))
        container_id = os.environ.get("CLICKHOUSE_CONTAINER_ID", "")
        if not container_id:
            raise DeletionError("CLICKHOUSE_CONTAINER_ID is required")
        client = maintenance.ClickHouseClient(
            container_id=container_id,
            user=os.environ.get("CLICKHOUSE_USER", "apdl"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", "apdl_dev"),
            database=os.environ.get("CLICKHOUSE_DB", "apdl"),
        )
        with maintenance._migration_lock(client.container_id, client.database):
            with maintenance._maintenance_fence() as fence:
                _assert_postgres_schema(fence)
                _assert_clickhouse_schema(client, fence)
                with maintenance._maintenance_owner(client, fence):
                    with maintenance._maintenance_writer_gate(client, fence):
                        result = execute_request(request, client, fence)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        DeletionError,
        maintenance.MigrationError,
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"Analytics deletion failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
