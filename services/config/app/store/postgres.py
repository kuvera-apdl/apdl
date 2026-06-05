"""asyncpg-based PostgreSQL store for flags and experiments.

All operations use parameterized queries to prevent SQL injection.
"""

import json
import logging

logger = logging.getLogger(__name__)


def _json_value(value, fallback):
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
        "owners": _json_value(row["owners"], []),
        "review_by": str(row["review_by"]) if row["review_by"] else None,
        "enabled": row["enabled"],
        "description": row["description"],
        "default_value": row["default_value"],
        "rules": _json_value(row["rules"], []),
        "fallthrough": _json_value(row["fallthrough"], {}),
        "salt": row["salt"],
        "evaluation_mode": row["evaluation_mode"],
        "auto_disable": row["auto_disable"],
        "guardrails": _json_value(row["guardrails"], []),
        "disabled_reason": row["disabled_reason"],
        "disabled_by": row["disabled_by"],
        "disabled_at": str(row["disabled_at"]) if row["disabled_at"] else None,
        "version": row["version"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "archived_at": str(row["archived_at"]) if row["archived_at"] else None,
    }


def _row_to_experiment(row) -> dict:
    """Convert an asyncpg Record to an experiment dict."""
    return {
        "key": row["key"],
        "project_id": row["project_id"],
        "status": row["status"],
        "description": row["description"],
        "variants_json": row["variants_json"],
        "targeting_rules_json": row["targeting_rules_json"],
        "traffic_percentage": float(row["traffic_percentage"]),
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
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

    sql = f"SELECT * FROM flags WHERE {' AND '.join(filters)} ORDER BY key"
    rows = await pool.fetch(sql, project_id)
    return [_row_to_flag(r) for r in rows]


async def get_flag(
    pool, project_id: str, key: str, *, include_archived: bool = False
) -> dict | None:
    """Fetch a single flag by project_id and key."""
    archived_filter = "" if include_archived else " AND archived_at IS NULL"
    sql = f"SELECT * FROM flags WHERE project_id = $1 AND key = $2{archived_filter}"
    row = await pool.fetchrow(sql, project_id, key)
    if row is None:
        return None
    return _row_to_flag(row)


async def create_flag(pool, flag: dict) -> dict | None:
    """Insert a new flag. Returns the inserted row, or None on failure."""
    sql = """
        INSERT INTO flags (
            key, project_id, name, state, owners, review_by, enabled,
            description, default_value, rules, fallthrough, salt,
            evaluation_mode, auto_disable, guardrails
        )
        VALUES (
            $1, $2, $3, $4, $5::jsonb, $6, $7,
            $8, $9, $10::jsonb, $11::jsonb, $12, $13, $14, $15::jsonb
        )
        RETURNING *
    """
    try:
        row = await pool.fetchrow(
            sql,
            flag["key"],
            flag["project_id"],
            flag["name"],
            flag.get("state", "draft"),
            json.dumps(flag.get("owners", []), separators=(",", ":")),
            flag.get("review_by"),
            flag.get("enabled", False),
            flag.get("description", ""),
            flag.get("default_value", False),
            json.dumps(flag.get("rules", []), separators=(",", ":")),
            json.dumps(flag.get("fallthrough", {}), separators=(",", ":")),
            flag["salt"],
            flag.get("evaluation_mode", "client"),
            flag.get("auto_disable", True),
            json.dumps(flag.get("guardrails", []), separators=(",", ":")),
        )
        return _row_to_flag(row)
    except Exception as exc:
        logger.error("createFlag failed: %s", exc)
        return None


async def update_flag(pool, flag: dict, expected_version: int) -> dict | None:
    """Update an existing flag using optimistic versioning."""
    sql = """
        UPDATE flags SET
            name = $4,
            state = $5,
            owners = $6::jsonb,
            review_by = $7,
            enabled = $8,
            description = $9,
            default_value = $10,
            rules = $11::jsonb,
            fallthrough = $12::jsonb,
            evaluation_mode = $13,
            auto_disable = $14,
            guardrails = $15::jsonb,
            version = version + 1,
            updated_at = NOW()
        WHERE project_id = $1
          AND key = $2
          AND version = $3
          AND archived_at IS NULL
        RETURNING *
    """
    try:
        row = await pool.fetchrow(
            sql,
            flag["project_id"],
            flag["key"],
            expected_version,
            flag["name"],
            flag["state"],
            json.dumps(flag["owners"], separators=(",", ":")),
            flag.get("review_by"),
            flag["enabled"],
            flag["description"],
            flag["default_value"],
            json.dumps(flag["rules"], separators=(",", ":")),
            json.dumps(flag["fallthrough"], separators=(",", ":")),
            flag["evaluation_mode"],
            flag["auto_disable"],
            json.dumps(flag["guardrails"], separators=(",", ":")),
        )
        return _row_to_flag(row) if row is not None else None
    except Exception as exc:
        logger.error("updateFlag failed: %s", exc)
        return None


async def archive_flag(
    pool,
    project_id: str,
    key: str,
    *,
    expected_version: int | None = None,
) -> dict | None:
    """Soft-archive a flag. Returns the archived row if it existed."""
    version_filter = "" if expected_version is None else " AND version = $3"
    sql = """
        UPDATE flags SET
            state = 'archived',
            enabled = false,
            archived_at = NOW(),
            version = version + 1,
            updated_at = NOW()
        WHERE project_id = $1 AND key = $2 AND archived_at IS NULL
    """ + version_filter + """
        RETURNING *
    """
    try:
        args = (project_id, key) if expected_version is None else (project_id, key, expected_version)
        row = await pool.fetchrow(sql, *args)
        return _row_to_flag(row) if row is not None else None
    except Exception as exc:
        logger.error("archiveFlag failed: %s", exc)
        return None


async def disable_flag(
    pool,
    *,
    project_id: str,
    key: str,
    reason: str,
    source: str,
) -> dict | None:
    """Disable an enabled flag and record disable metadata."""
    sql = """
        UPDATE flags SET
            state = 'disabled',
            enabled = false,
            disabled_reason = $3,
            disabled_by = $4,
            disabled_at = NOW(),
            version = version + 1,
            updated_at = NOW()
        WHERE project_id = $1
          AND key = $2
          AND enabled = true
          AND archived_at IS NULL
        RETURNING *
    """
    try:
        row = await pool.fetchrow(sql, project_id, key, reason, source)
        return _row_to_flag(row) if row is not None else None
    except Exception as exc:
        logger.error("disableFlag failed: %s", exc)
        return None


async def create_flag_audit_entry(
    pool,
    *,
    project_id: str,
    flag_key: str,
    action: str,
    actor: str,
    before: dict | None,
    after: dict | None,
    reason: str = "",
    evidence: dict | None = None,
) -> None:
    """Append a flag audit event."""
    sql = """
        INSERT INTO flag_audit_log (
            project_id, flag_key, action, actor, previous_version,
            new_version, before, after, evidence, reason
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9::jsonb, $10)
    """
    await pool.execute(
        sql,
        project_id,
        flag_key,
        action,
        actor,
        before.get("version") if before else None,
        after.get("version") if after else None,
        json.dumps(before, separators=(",", ":")) if before else None,
        json.dumps(after, separators=(",", ":")) if after else None,
        json.dumps(evidence or {}, separators=(",", ":")),
        reason,
    )


async def get_flag_audit_entries(
    pool,
    project_id: str,
    flag_key: str,
    *,
    limit: int = 50,
) -> list[dict]:
    """Fetch recent audit entries for a flag."""
    sql = """
        SELECT id, project_id, flag_key, action, actor, previous_version,
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
            "previous_version": row["previous_version"],
            "new_version": row["new_version"],
            "before": _json_value(row["before"], None),
            "after": _json_value(row["after"], None),
            "evidence": _json_value(row["evidence"], {}),
            "reason": row["reason"],
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


# ---- Experiment operations ----


async def get_experiments(pool, project_id: str) -> list[dict]:
    """Fetch all experiments for a project, ordered by key."""
    sql = "SELECT * FROM experiments WHERE project_id = $1 ORDER BY key"
    rows = await pool.fetch(sql, project_id)
    return [_row_to_experiment(r) for r in rows]


async def get_experiment(pool, project_id: str, key: str) -> dict | None:
    """Fetch a single experiment by project_id and key."""
    sql = "SELECT * FROM experiments WHERE project_id = $1 AND key = $2"
    row = await pool.fetchrow(sql, project_id, key)
    if row is None:
        return None
    return _row_to_experiment(row)


async def create_experiment(pool, exp: dict) -> bool:
    """Insert a new experiment. Returns True on success."""
    sql = """
        INSERT INTO experiments (key, project_id, status, description, variants_json,
                                  targeting_rules_json, traffic_percentage, start_date, end_date)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    """
    try:
        await pool.execute(
            sql,
            exp["key"],
            exp["project_id"],
            exp.get("status", "draft"),
            exp.get("description", ""),
            exp.get("variants_json", "[]"),
            exp.get("targeting_rules_json", "[]"),
            exp.get("traffic_percentage", 100.0),
            exp.get("start_date", ""),
            exp.get("end_date", ""),
        )
        return True
    except Exception as exc:
        logger.error("createExperiment failed: %s", exc)
        return False


async def update_experiment(pool, exp: dict) -> bool:
    """Update an existing experiment. Returns True if a row was modified."""
    sql = """
        UPDATE experiments SET
            status = $3,
            description = $4,
            variants_json = $5,
            targeting_rules_json = $6,
            traffic_percentage = $7,
            start_date = $8,
            end_date = $9,
            updated_at = NOW()
        WHERE project_id = $1 AND key = $2
    """
    try:
        result = await pool.execute(
            sql,
            exp["project_id"],
            exp["key"],
            exp.get("status", "draft"),
            exp.get("description", ""),
            exp.get("variants_json", "[]"),
            exp.get("targeting_rules_json", "[]"),
            exp.get("traffic_percentage", 100.0),
            exp.get("start_date", ""),
            exp.get("end_date", ""),
        )
        return result.endswith("1")
    except Exception as exc:
        logger.error("updateExperiment failed: %s", exc)
        return False


async def delete_experiment(pool, project_id: str, key: str) -> bool:
    """Delete an experiment. Returns True if a row was deleted."""
    sql = "DELETE FROM experiments WHERE project_id = $1 AND key = $2"
    try:
        result = await pool.execute(sql, project_id, key)
        return result.endswith("1")
    except Exception as exc:
        logger.error("deleteExperiment failed: %s", exc)
        return False
