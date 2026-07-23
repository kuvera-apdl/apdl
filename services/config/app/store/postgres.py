"""Read-only asyncpg projections for flags and experiments.

All writes belong to :mod:`app.store.mutations`, which commits the domain row,
audit record, and durable outbox intent in one transaction.
"""

import json

DEFAULT_VARIANTS = [
    {"key": "control", "weight": 1},
    {"key": "treatment", "weight": 1},
]
DEFAULT_FALLTHROUGH = {
    "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
}
FLAG_COLUMNS = """
    key, project_id, name, state, owners, review_by, enabled,
    description, default_variant, variants, rules, fallthrough, salt,
    evaluation_mode, auto_disable, guardrails, disabled_reason, disabled_by,
    disabled_at, version, created_at, updated_at, archived_at
"""
EXPERIMENT_COLUMNS = """
    key, project_id, status, description, flag_key, default_variant,
    variants_json, targeting_rules_json, primary_metric_json, statistical_plan,
    traffic_percentage, bucket_by, minimum_exposure_config_version,
    start_date, end_date, version, creation_idempotency_key,
    creation_idempotency_request_sha256, created_at, updated_at, archived_at,
    archived_by
"""


def _json_field(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


def _row_to_flag(row) -> dict:
    """Convert an asyncpg Record to a flag dict."""
    return {
        "key": row["key"],
        "project_id": row["project_id"],
        "name": row["name"],
        "state": row["state"],
        "owners": _json_field(row["owners"], []),
        "review_by": str(row["review_by"]) if row["review_by"] else None,
        "enabled": row["enabled"],
        "description": row["description"],
        "default_variant": row["default_variant"],
        "variants": _json_field(row["variants"], []),
        "rules": _json_field(row["rules"], []),
        "fallthrough": _json_field(row["fallthrough"], {}),
        "salt": row["salt"],
        "evaluation_mode": row["evaluation_mode"],
        "auto_disable": row["auto_disable"],
        "guardrails": _json_field(row["guardrails"], []),
        "disabled_reason": row["disabled_reason"],
        "disabled_by": row["disabled_by"],
        "disabled_at": str(row["disabled_at"]) if row["disabled_at"] else None,
        "version": row["version"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "archived_at": str(row["archived_at"]) if row["archived_at"] else None,
    }


def _row_to_experiment(row) -> dict:
    """Convert an asyncpg Record without erasing database timestamp types.

    Experiment records are reused by the transactional mutation authority, so
    ``TIMESTAMPTZ`` values must remain ``datetime`` instances until an API
    serializer explicitly formats them for transport.
    """
    return {
        "key": row["key"],
        "project_id": row["project_id"],
        "status": row["status"],
        "description": row["description"],
        "flag_key": row["flag_key"],
        "default_variant": row["default_variant"],
        "variants_json": row["variants_json"],
        "targeting_rules_json": row["targeting_rules_json"],
        "primary_metric_json": row["primary_metric_json"],
        "statistical_plan": _json_field(row["statistical_plan"], None),
        "traffic_percentage": float(row["traffic_percentage"]),
        "bucket_by": row["bucket_by"],
        "minimum_exposure_config_version": row[
            "minimum_exposure_config_version"
        ],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "version": row["version"],
        "creation_idempotency_key": row.get("creation_idempotency_key"),
        "creation_idempotency_request_sha256": row.get(
            "creation_idempotency_request_sha256"
        ),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row["archived_at"],
        "archived_by": row["archived_by"],
    }


# ---- Flag operations ----


async def get_flags(
    pool,
    project_id: str,
    *,
    include_archived: bool = False,
    client_visible_only: bool = False,
) -> list[dict]:
    """Fetch all flags for a project, ordered by key."""
    filters = ["project_id = $1"]
    if not include_archived:
        filters.append("archived_at IS NULL")
    if client_visible_only:
        filters.append("evaluation_mode IN ('client', 'both')")

    sql = f"SELECT {FLAG_COLUMNS} FROM flags WHERE {' AND '.join(filters)} ORDER BY key"
    rows = await pool.fetch(sql, project_id)
    return [_row_to_flag(r) for r in rows]


async def get_flag_snapshot(
    pool,
    project_id: str,
    *,
    client_visible_only: bool = False,
) -> tuple[list[dict], int]:
    """Read one flag collection and its project version from one DB snapshot."""
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            project_version = await conn.fetchval(
                """
                SELECT COALESCE(
                    (
                        SELECT project_version
                        FROM config_project_versions
                        WHERE project_id = $1
                    ),
                    0
                )
                """,
                project_id,
            )
            flags = await get_flags(
                conn,
                project_id,
                client_visible_only=client_visible_only,
            )
    return flags, int(project_version)


async def get_flag(
    pool, project_id: str, key: str, *, include_archived: bool = False
) -> dict | None:
    """Fetch a single flag by project_id and key."""
    archived_filter = "" if include_archived else " AND archived_at IS NULL"
    sql = (
        f"SELECT {FLAG_COLUMNS} FROM flags "
        f"WHERE project_id = $1 AND key = $2{archived_filter}"
    )
    row = await pool.fetchrow(sql, project_id, key)
    if row is None:
        return None
    return _row_to_flag(row)


async def get_flag_audit_entries(
    pool,
    project_id: str,
    flag_key: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """Fetch recent audit entries for a flag."""
    sql = """
        SELECT id, project_id, flag_key, action, actor, origin, previous_version,
               new_version, before, after, evidence, reason, created_at
        FROM flag_audit_log
        WHERE project_id = $1 AND flag_key = $2
        ORDER BY created_at DESC, id DESC
        LIMIT $3
    """
    rows = await pool.fetch(sql, project_id, flag_key, limit)
    return [
        {
            "id": row["id"],
            "project_id": row["project_id"],
            "flag_key": row["flag_key"],
            "action": row["action"],
            "actor": row["actor"],
            "origin": row["origin"],
            "previous_version": row["previous_version"],
            "new_version": row["new_version"],
            "before": _json_field(row["before"], None),
            "after": _json_field(row["after"], None),
            "evidence": _json_field(row["evidence"], {}),
            "reason": row["reason"],
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


# ---- Experiment operations ----


async def get_experiments(pool, project_id: str) -> list[dict]:
    """Fetch all experiments for a project, ordered by key."""
    sql = f"SELECT {EXPERIMENT_COLUMNS} FROM experiments WHERE project_id = $1 ORDER BY key"
    rows = await pool.fetch(sql, project_id)
    return [_row_to_experiment(r) for r in rows]


async def get_due_experiments(pool, now) -> list[dict]:
    """Return scheduled starts and running completions due at ``now``."""
    sql = (
        f"SELECT {EXPERIMENT_COLUMNS} FROM experiments "
        "WHERE archived_at IS NULL AND ("
        "       (status = 'scheduled' AND start_date <= $1) "
        "    OR (status = 'running' AND end_date <= $1)"
        ") "
        "ORDER BY project_id, key"
    )
    rows = await pool.fetch(sql, now)
    return [_row_to_experiment(row) for row in rows]


async def get_experiment(pool, project_id: str, key: str) -> dict | None:
    """Fetch a single experiment by project_id and key."""
    sql = f"SELECT {EXPERIMENT_COLUMNS} FROM experiments WHERE project_id = $1 AND key = $2"
    row = await pool.fetchrow(sql, project_id, key)
    if row is None:
        return None
    return _row_to_experiment(row)


async def get_experiment_by_creation_idempotency_key(
    pool, project_id: str, idempotency_key: str
) -> dict | None:
    """Fetch the canonical result of a retried experiment-create command."""
    sql = (
        f"SELECT {EXPERIMENT_COLUMNS} FROM experiments "
        "WHERE project_id = $1 AND creation_idempotency_key = $2"
    )
    row = await pool.fetchrow(sql, project_id, idempotency_key)
    return _row_to_experiment(row) if row is not None else None


async def get_experiment_audit_entries(
    pool,
    project_id: str,
    experiment_key: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """Fetch retained lifecycle evidence for an experiment."""
    rows = await pool.fetch(
        """
        SELECT id, project_id, experiment_key, action, actor,
               previous_version, new_version, before, after, created_at
        FROM experiment_audit_log
        WHERE project_id = $1 AND experiment_key = $2
        ORDER BY created_at DESC, id DESC
        LIMIT $3
        """,
        project_id,
        experiment_key,
        limit,
    )
    return [
        {
            "id": row["id"],
            "project_id": row["project_id"],
            "experiment_key": row["experiment_key"],
            "action": row["action"],
            "actor": row["actor"],
            "previous_version": row["previous_version"],
            "new_version": row["new_version"],
            "before": _json_field(row["before"], None),
            "after": _json_field(row["after"], None),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]
