"""Transactional authority for Config mutations.

Every public write commits its database row, optimistic version, audit record,
and durable delivery intent on one PostgreSQL connection. Redis and SSE are
deliberately absent from this module; the outbox worker owns those effects.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.flags import experiment_flag
from app.models.schemas import (
    ExperimentMetric,
    ExperimentStatisticalPlan,
    GateRule,
    VariantConfig,
    validate_flag_variant_config,
    validate_statistical_plan,
)
from app.store import postgres as pg_store
from app.utils import serialize_client_flag


class MutationError(RuntimeError):
    """Base class for expected command failures."""


class NotFoundError(MutationError):
    def __init__(self, entity: str, key: str):
        super().__init__(f"{entity} '{key}' not found")
        self.entity = entity
        self.key = key


class VersionConflictError(MutationError):
    def __init__(self, entity: str, key: str, current_version: int):
        super().__init__(f"{entity} '{key}' is at version {current_version}")
        self.entity = entity
        self.key = key
        self.current_version = current_version


class ExperimentOwnedFlagError(MutationError):
    def __init__(self, flag_key: str, experiment_key: str):
        super().__init__(
            f"Flag '{flag_key}' is managed by experiment '{experiment_key}'"
        )
        self.flag_key = flag_key
        self.experiment_key = experiment_key


class IntegrityError(MutationError):
    """A persisted Config relationship is internally inconsistent."""


class ImmutableExperimentError(MutationError):
    """An update attempted to rewrite an experiment's analysis contract."""

    def __init__(self, key: str, fields: list[str]):
        ordered_fields = sorted(fields)
        super().__init__(
            f"Experiment '{key}' analysis fields are immutable after draft: "
            f"{', '.join(ordered_fields)}"
        )
        self.key = key
        self.fields = ordered_fields


class ArchivedExperimentError(MutationError):
    """A mutation targeted an immutable archived experiment."""

    def __init__(self, key: str):
        super().__init__(f"Experiment '{key}' is archived and immutable")
        self.key = key


_FROZEN_EXPERIMENT_FIELDS = {
    "default_variant": "default_variant",
    "traffic_percentage": "traffic_percentage",
    "targeting_rules_json": "targeting_rules",
    "variants_json": "variants",
    "primary_metric_json": "primary_metric",
    "statistical_plan": "statistical_plan",
    "start_date": "start_date",
    "end_date": "end_date",
}


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


async def _insert_outbox(
    conn,
    *,
    project_id: str,
    kind: str,
    dedup_key: str,
    payload: dict,
) -> None:
    await conn.execute(
        """
        INSERT INTO config_outbox (project_id, kind, dedup_key, payload)
        VALUES ($1, $2, $3, $4::jsonb)
        """,
        project_id,
        kind,
        dedup_key,
        _json(payload),
    )


async def _next_project_version(conn, project_id: str) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO config_project_versions (project_id, project_version)
        VALUES ($1, 1)
        ON CONFLICT (project_id) DO UPDATE
        SET project_version = config_project_versions.project_version + 1,
            updated_at = now()
        RETURNING project_version
        """,
        project_id,
    )
    return int(row["project_version"])


def _flag_delivery(action: str, flag: dict, project_version: int) -> dict:
    version = int(flag["version"])
    if (
        flag.get("evaluation_mode") in {"client", "both"}
        and not flag.get("archived_at")
    ):
        data = {
            "action": action,
            "flag": serialize_client_flag(flag),
            "version": version,
        }
    else:
        data = {
            "action": "flag_removed",
            "key": flag["key"],
            "version": version,
        }
    return {
        "event_type": "flag_update",
        "project_version": project_version,
        "data": data,
    }


def _experiment_delivery(
    action: str,
    experiment: dict,
    project_version: int,
) -> dict:
    version = int(experiment["version"])
    return {
        "event_type": "experiment_update",
        "data": {
            "action": action,
            "key": experiment["key"],
            "status": experiment.get("status"),
            "flag_key": experiment["flag_key"],
            "version": version,
        },
        "project_version": project_version,
    }


async def _enqueue_flag_change(
    conn,
    action: str,
    flag: dict,
    *,
    project_version: int | None = None,
) -> int:
    if project_version is None:
        project_version = await _next_project_version(conn, flag["project_id"])
    await _insert_outbox(
        conn,
        project_id=flag["project_id"],
        kind="flag_change",
        dedup_key=f"{flag['key']}:{flag['version']}:{action}",
        payload=_flag_delivery(action, flag, project_version),
    )
    return project_version


async def _enqueue_experiment_change(
    conn,
    action: str,
    experiment: dict,
    *,
    project_version: int | None = None,
) -> int:
    if project_version is None:
        project_version = await _next_project_version(
            conn,
            experiment["project_id"],
        )
    await _insert_outbox(
        conn,
        project_id=experiment["project_id"],
        kind="experiment_change",
        dedup_key=(
            f"{experiment['key']}:{experiment['version']}:{action}"
        ),
        payload=_experiment_delivery(action, experiment, project_version),
    )
    return project_version


async def _audit_flag(
    conn,
    *,
    action: str,
    actor: str,
    origin: str,
    before: dict | None,
    after: dict | None,
    reason: str = "",
    evidence: dict | None = None,
) -> None:
    flag = after or before
    if flag is None:
        raise IntegrityError("flag audit requires a before or after snapshot")
    await conn.execute(
        """
        INSERT INTO flag_audit_log (
            project_id, flag_key, action, actor, origin, previous_version,
            new_version, before, after, evidence, reason
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7,
            $8::jsonb, $9::jsonb, $10::jsonb, $11
        )
        """,
        flag["project_id"],
        flag["key"],
        action,
        actor,
        origin,
        before.get("version") if before else None,
        after.get("version") if after else None,
        _json(before) if before else None,
        _json(after) if after else None,
        _json(evidence or {}),
        reason,
    )


async def _audit_experiment(
    conn,
    *,
    action: str,
    actor: str,
    before: dict | None,
    after: dict | None,
) -> None:
    experiment = after or before
    if experiment is None:
        raise IntegrityError("experiment audit requires a before or after snapshot")
    await conn.execute(
        """
        INSERT INTO experiment_audit_log (
            project_id, experiment_key, action, actor, previous_version,
            new_version, before, after
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)
        """,
        experiment["project_id"],
        experiment["key"],
        action,
        actor,
        before.get("version") if before else None,
        after.get("version") if after else None,
        _json(before) if before else None,
        _json(after) if after else None,
    )


async def _locked_flag(conn, project_id: str, key: str) -> dict:
    row = await conn.fetchrow(
        f"""
        SELECT {pg_store.FLAG_COLUMNS}
        FROM flags
        WHERE project_id = $1 AND key = $2 AND archived_at IS NULL
        FOR UPDATE
        """,
        project_id,
        key,
    )
    if row is None:
        raise NotFoundError("Flag", key)
    return pg_store._row_to_flag(row)


async def _locked_experiment(conn, project_id: str, key: str) -> dict:
    row = await conn.fetchrow(
        f"""
        SELECT {pg_store.EXPERIMENT_COLUMNS}
        FROM experiments
        WHERE project_id = $1 AND key = $2
        FOR UPDATE
        """,
        project_id,
        key,
    )
    if row is None:
        raise NotFoundError("Experiment", key)
    return pg_store._row_to_experiment(row)


async def _reject_experiment_owned(conn, project_id: str, flag_key: str) -> None:
    owner = await conn.fetchrow(
        """
        SELECT key
        FROM experiments
        WHERE project_id = $1 AND flag_key = $2
        """,
        project_id,
        flag_key,
    )
    if owner is not None:
        raise ExperimentOwnedFlagError(flag_key, str(owner["key"]))


async def _insert_flag(conn, flag: dict) -> dict:
    validate_flag_variant_config(flag)
    row = await conn.fetchrow(
        f"""
        INSERT INTO flags (
            key, project_id, name, state, owners, review_by, enabled,
            description, default_variant, variants, rules, fallthrough, salt,
            evaluation_mode, auto_disable, guardrails
        )
        VALUES (
            $1, $2, $3, $4, $5::jsonb, $6, $7,
            $8, $9, $10::jsonb, $11::jsonb, $12::jsonb, $13, $14, $15,
            $16::jsonb
        )
        RETURNING {pg_store.FLAG_COLUMNS}
        """,
        flag["key"],
        flag["project_id"],
        flag["name"],
        flag.get("state", "draft"),
        _json(flag.get("owners", [])),
        flag.get("review_by"),
        flag.get("enabled", False),
        flag.get("description", ""),
        flag.get("default_variant", "control"),
        _json(flag.get("variants", pg_store.DEFAULT_VARIANTS)),
        _json(flag.get("rules", [])),
        _json(flag.get("fallthrough", pg_store.DEFAULT_FALLTHROUGH)),
        flag["salt"],
        flag.get("evaluation_mode", "client"),
        flag.get("auto_disable", False),
        _json(flag.get("guardrails", [])),
    )
    return pg_store._row_to_flag(row)


async def _update_flag(conn, flag: dict, expected_version: int) -> dict:
    validate_flag_variant_config(flag)
    row = await conn.fetchrow(
        f"""
        UPDATE flags SET
            name = $4,
            state = $5,
            owners = $6::jsonb,
            review_by = $7,
            enabled = $8,
            description = $9,
            default_variant = $10,
            variants = $11::jsonb,
            rules = $12::jsonb,
            fallthrough = $13::jsonb,
            evaluation_mode = $14,
            auto_disable = $15,
            guardrails = $16::jsonb,
            version = version + 1,
            updated_at = now()
        WHERE project_id = $1 AND key = $2 AND version = $3
          AND archived_at IS NULL
        RETURNING {pg_store.FLAG_COLUMNS}
        """,
        flag["project_id"],
        flag["key"],
        expected_version,
        flag["name"],
        flag["state"],
        _json(flag["owners"]),
        flag.get("review_by"),
        flag["enabled"],
        flag["description"],
        flag["default_variant"],
        _json(flag["variants"]),
        _json(flag["rules"]),
        _json(flag["fallthrough"]),
        flag["evaluation_mode"],
        flag["auto_disable"],
        _json(flag["guardrails"]),
    )
    if row is None:
        raise VersionConflictError("Flag", flag["key"], expected_version)
    return pg_store._row_to_flag(row)


async def _transition_flag(
    conn,
    before: dict,
    target_state: str,
) -> dict:
    row = await conn.fetchrow(
        f"""
        UPDATE flags SET
            state = $4,
            enabled = ($4 = 'active'),
            disabled_reason = '',
            disabled_by = '',
            disabled_at = NULL,
            version = version + 1,
            updated_at = now()
        WHERE project_id = $1 AND key = $2 AND version = $3
          AND archived_at IS NULL
        RETURNING {pg_store.FLAG_COLUMNS}
        """,
        before["project_id"],
        before["key"],
        before["version"],
        target_state,
    )
    if row is None:
        raise VersionConflictError("Flag", before["key"], before["version"])
    return pg_store._row_to_flag(row)


async def _disable_flag(
    conn,
    before: dict,
    *,
    reason: str,
    actor: str,
) -> dict:
    row = await conn.fetchrow(
        f"""
        UPDATE flags SET
            state = 'disabled',
            enabled = false,
            disabled_reason = $4,
            disabled_by = $5,
            disabled_at = now(),
            version = version + 1,
            updated_at = now()
        WHERE project_id = $1 AND key = $2 AND version = $3
          AND archived_at IS NULL
        RETURNING {pg_store.FLAG_COLUMNS}
        """,
        before["project_id"],
        before["key"],
        before["version"],
        reason,
        actor,
    )
    if row is None:
        raise VersionConflictError("Flag", before["key"], before["version"])
    return pg_store._row_to_flag(row)


async def _archive_flag(conn, before: dict) -> dict:
    row = await conn.fetchrow(
        f"""
        UPDATE flags SET
            state = 'archived',
            enabled = false,
            archived_at = now(),
            version = version + 1,
            updated_at = now()
        WHERE project_id = $1 AND key = $2 AND version = $3
          AND archived_at IS NULL
        RETURNING {pg_store.FLAG_COLUMNS}
        """,
        before["project_id"],
        before["key"],
        before["version"],
    )
    if row is None:
        raise VersionConflictError("Flag", before["key"], before["version"])
    return pg_store._row_to_flag(row)


async def _insert_experiment(conn, experiment: dict) -> dict:
    row = await conn.fetchrow(
        f"""
        INSERT INTO experiments (
            key, project_id, status, description, flag_key, default_variant,
            variants_json, targeting_rules_json, primary_metric_json, statistical_plan,
            traffic_percentage, start_date, end_date, creation_idempotency_key,
            creation_idempotency_request_sha256
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13, $14,
            $15
        )
        RETURNING {pg_store.EXPERIMENT_COLUMNS}
        """,
        experiment["key"],
        experiment["project_id"],
        experiment.get("status", "draft"),
        experiment.get("description", ""),
        experiment["flag_key"],
        experiment.get("default_variant", "control"),
        experiment.get("variants_json", "[]"),
        experiment.get("targeting_rules_json", "[]"),
        experiment.get("primary_metric_json", "{}"),
        _json(experiment.get("statistical_plan"))
        if experiment.get("statistical_plan") is not None
        else None,
        experiment.get("traffic_percentage", 100.0),
        experiment.get("start_date"),
        experiment.get("end_date"),
        experiment.get("creation_idempotency_key"),
        experiment.get("creation_idempotency_request_sha256"),
    )
    return pg_store._row_to_experiment(row)


async def _update_experiment(
    conn,
    experiment: dict,
    expected_version: int,
) -> dict:
    row = await conn.fetchrow(
        f"""
        UPDATE experiments SET
            status = $4,
            description = $5,
            default_variant = $6,
            variants_json = $7,
            targeting_rules_json = $8,
            primary_metric_json = $9,
            statistical_plan = $10::jsonb,
            traffic_percentage = $11,
            start_date = $12,
            end_date = $13,
            version = version + 1,
            updated_at = now()
        WHERE project_id = $1 AND key = $2 AND version = $3
        RETURNING {pg_store.EXPERIMENT_COLUMNS}
        """,
        experiment["project_id"],
        experiment["key"],
        expected_version,
        experiment["status"],
        experiment["description"],
        experiment["default_variant"],
        experiment["variants_json"],
        experiment["targeting_rules_json"],
        experiment["primary_metric_json"],
        _json(experiment.get("statistical_plan"))
        if experiment.get("statistical_plan") is not None
        else None,
        experiment["traffic_percentage"],
        experiment.get("start_date"),
        experiment.get("end_date"),
    )
    if row is None:
        raise VersionConflictError(
            "Experiment",
            experiment["key"],
            expected_version,
        )
    return pg_store._row_to_experiment(row)


async def _archive_experiment(conn, before: dict, *, actor: str) -> dict:
    archive_time = datetime.now(timezone.utc)
    desired = before
    if before["status"] in {"scheduled", "running"}:
        desired, _ = finalize_terminal_analysis_window(
            before,
            {**before, "status": "stopped"},
            now=archive_time,
        )
    row = await conn.fetchrow(
        f"""
        UPDATE experiments SET
            status = $4,
            end_date = $5,
            archived_at = $6,
            archived_by = $7,
            version = version + 1,
            updated_at = now()
        WHERE project_id = $1 AND key = $2 AND version = $3
          AND status <> 'draft' AND archived_at IS NULL
        RETURNING {pg_store.EXPERIMENT_COLUMNS}
        """,
        before["project_id"],
        before["key"],
        before["version"],
        desired["status"],
        desired.get("end_date"),
        archive_time,
        actor,
    )
    if row is None:
        raise VersionConflictError(
            "Experiment",
            before["key"],
            before["version"],
        )
    return pg_store._row_to_experiment(row)


async def _delete_draft_experiment(conn, before: dict) -> None:
    result = await conn.execute(
        """
        DELETE FROM experiments
        WHERE project_id = $1 AND key = $2 AND version = $3
          AND status = 'draft' AND archived_at IS NULL
        """,
        before["project_id"],
        before["key"],
        before["version"],
    )
    if not result.endswith("1"):
        raise VersionConflictError(
            "Experiment",
            before["key"],
            before["version"],
        )


def _load_json(raw: str | None, fallback):
    if not raw:
        return fallback
    return json.loads(raw)


def _frozen_value(field: str, value: Any) -> Any:
    if field in {
        "variants_json",
        "primary_metric_json",
        "statistical_plan",
    } and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if field in {"start_date", "end_date"}:
        try:
            return _parse_timestamp(value)
        except ValueError:
            return value
    return value


def ensure_experiment_analysis_fields_immutable(
    before: dict,
    desired: dict,
    *,
    allowed_fields: frozenset[str] = frozenset(),
) -> None:
    """Reject analysis-contract changes once an experiment leaves draft."""
    if before["status"] == "draft":
        return
    changed = [
        api_field
        for stored_field, api_field in _FROZEN_EXPERIMENT_FIELDS.items()
        if stored_field not in allowed_fields
        if _frozen_value(stored_field, before.get(stored_field))
        != _frozen_value(stored_field, desired.get(stored_field))
    ]
    if changed:
        raise ImmutableExperimentError(before["key"], changed)


def finalize_terminal_analysis_window(
    before: dict,
    desired: dict,
    *,
    now: datetime | None = None,
) -> tuple[dict, frozenset[str]]:
    """Preserve fixed completion horizons and truncate only stopped windows."""
    if before["status"] in {"draft", "scheduled"} and desired["status"] == "stopped":
        # A cancelled experiment never opened an observation window.
        # Persist no analysis end so the analysis projection fails closed
        # permanently instead of collecting future events after cancellation.
        return {**desired, "end_date": None}, frozenset({"end_date"})

    if before["status"] != "running" or desired["status"] not in {
        "completed",
        "stopped",
    }:
        return desired, frozenset()

    current = now or datetime.now(timezone.utc)
    start = _parse_timestamp(before.get("start_date"))
    planned_end = _parse_timestamp(before.get("end_date"))
    if (
        start is None
        or planned_end is None
        or start.tzinfo is None
        or planned_end.tzinfo is None
        or current.tzinfo is None
    ):
        raise IntegrityError("Running experiment has an invalid analysis window")
    if desired["status"] == "completed":
        if current < planned_end:
            raise IntegrityError(
                "A fixed-horizon experiment cannot complete before its planned end"
            )
        return {**desired, "end_date": planned_end}, frozenset()

    actual_end = min(current, planned_end)
    if actual_end <= start:
        raise IntegrityError("Terminal experiment end must be after its start")
    return {**desired, "end_date": actual_end}, frozenset({"end_date"})


def _derived_experiment_flag(experiment: dict, backing: dict) -> dict:
    variants = [
        VariantConfig.model_validate(
            {"key": value["key"], "weight": value["weight"]}
        )
        for value in _load_json(experiment.get("variants_json"), [])
    ]
    rules = [
        GateRule.model_validate(value)
        for value in _load_json(experiment.get("targeting_rules_json"), [])
    ]
    updates = experiment_flag.build_flag_projection(
        flag_key=backing["key"],
        name=backing.get("name") or backing["key"],
        description=experiment.get("description", ""),
        status=experiment["status"],
        variants=variants,
        default_variant=experiment["default_variant"],
        traffic_percentage=float(experiment["traffic_percentage"]),
        targeting_rules=rules,
    )
    merged = {**backing, **updates}
    validate_flag_variant_config(merged)
    return merged


async def create_standalone_flag(pool, flag: dict, *, actor: str) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            created = await _insert_flag(conn, flag)
            await _audit_flag(
                conn,
                action="flag_created",
                actor=actor,
                origin="manual",
                before=None,
                after=created,
            )
            await _enqueue_flag_change(conn, "flag_created", created)
            return created


async def update_standalone_flag(
    pool,
    *,
    project_id: str,
    key: str,
    expected_version: int,
    updates: dict,
    actor: str,
) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await _locked_flag(conn, project_id, key)
            await _reject_experiment_owned(conn, project_id, key)
            if before["version"] != expected_version:
                raise VersionConflictError(
                    "Flag",
                    key,
                    before["version"],
                )
            merged = {**before, **updates}
            validate_flag_variant_config(merged)
            updated = await _update_flag(conn, merged, expected_version)
            await _audit_flag(
                conn,
                action="flag_updated",
                actor=actor,
                origin="manual",
                before=before,
                after=updated,
            )
            await _enqueue_flag_change(conn, "flag_updated", updated)
            return updated


async def transition_standalone_flag(
    pool,
    *,
    project_id: str,
    key: str,
    expected_version: int,
    target_state: str,
    actor: str,
) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await _locked_flag(conn, project_id, key)
            await _reject_experiment_owned(conn, project_id, key)
            if before["version"] != expected_version:
                raise VersionConflictError("Flag", key, before["version"])
            updated = await _transition_flag(conn, before, target_state)
            await _audit_flag(
                conn,
                action="flag_updated",
                actor=actor,
                origin="manual",
                before=before,
                after=updated,
                reason=f"transition_to_{target_state}",
            )
            await _enqueue_flag_change(conn, "flag_updated", updated)
            return updated


async def disable_standalone_flag(
    pool,
    *,
    project_id: str,
    key: str,
    expected_version: int,
    reason: str,
    evidence: dict,
    actor: str,
) -> tuple[dict, bool]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await _locked_flag(conn, project_id, key)
            await _reject_experiment_owned(conn, project_id, key)
            if before["version"] != expected_version:
                raise VersionConflictError("Flag", key, before["version"])
            if not before.get("enabled", False):
                return before, False
            updated = await _disable_flag(
                conn,
                before,
                reason=reason,
                actor=actor,
            )
            await _audit_flag(
                conn,
                action="flag_disabled",
                actor=actor,
                origin="manual",
                before=before,
                after=updated,
                reason=reason,
                evidence=evidence,
            )
            await _enqueue_flag_change(conn, "flag_updated", updated)
            return updated, True


async def archive_standalone_flag(
    pool,
    *,
    project_id: str,
    key: str,
    expected_version: int,
    actor: str,
) -> dict:
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await _locked_flag(conn, project_id, key)
            await _reject_experiment_owned(conn, project_id, key)
            if before["version"] != expected_version:
                raise VersionConflictError("Flag", key, before["version"])
            archived = await _archive_flag(conn, before)
            await _audit_flag(
                conn,
                action="flag_archived",
                actor=actor,
                origin="manual",
                before=before,
                after=archived,
            )
            await _enqueue_flag_change(conn, "flag_archived", archived)
            return archived


async def cleanup_standalone_flag(
    pool,
    *,
    project_id: str,
    key: str,
    expected_version: int,
    evidence: dict,
    actor: str,
) -> tuple[dict, list[str]]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            before = await _locked_flag(conn, project_id, key)
            await _reject_experiment_owned(conn, project_id, key)
            if before["version"] != expected_version:
                raise VersionConflictError("Flag", key, before["version"])
            reasons = _cleanup_reasons(before)
            if "fully_rolled_out" not in reasons:
                raise IntegrityError(f"Flag '{key}' is not eligible for cleanup")
            archived = await _archive_flag(conn, before)
            await _audit_flag(
                conn,
                action="flag_cleanup_archived",
                actor=actor,
                origin="manual",
                before=before,
                after=archived,
                reason="fully_rolled_out",
                evidence={**evidence, "cleanup_reasons": reasons},
            )
            await _enqueue_flag_change(conn, "flag_archived", archived)
            return archived, reasons


def _cleanup_reasons(flag: dict) -> list[str]:
    if flag.get("state") != "active" or not flag.get("enabled", False):
        return []
    if flag.get("rules"):
        return []
    rollout = flag.get("fallthrough", {}).get("rollout", {})
    percentage = rollout.get("percentage")
    if (
        isinstance(percentage, bool)
        or not isinstance(percentage, int | float)
        or percentage < 100
    ):
        return []
    positive = [
        value.get("key")
        for value in flag.get("variants", [])
        if isinstance(value, dict)
        and isinstance(value.get("weight"), int)
        and value["weight"] > 0
    ]
    if len(positive) == 1 and positive[0] != flag.get("default_variant"):
        return ["fully_rolled_out"]
    return []


async def create_experiment_bundle(
    pool,
    *,
    experiment: dict,
    flag: dict,
    actor: str,
) -> tuple[dict, dict]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            created_flag = await _insert_flag(conn, flag)
            created_experiment = await _insert_experiment(conn, experiment)
            await _audit_experiment(
                conn,
                action="experiment_created",
                actor=actor,
                before=None,
                after=created_experiment,
            )
            await _audit_flag(
                conn,
                action="flag_created",
                actor=actor,
                origin="experiment",
                before=None,
                after=created_flag,
                reason=f"experiment:{created_experiment['key']}",
            )
            project_version = await _enqueue_flag_change(
                conn,
                "flag_created",
                created_flag,
            )
            await _enqueue_experiment_change(
                conn,
                "experiment_created",
                created_experiment,
                project_version=project_version,
            )
            return created_experiment, created_flag


async def _update_experiment_bundle(
    conn,
    *,
    desired: dict,
    expected_version: int,
    actor: str,
    origin: str,
) -> tuple[dict, dict]:
    before_experiment = await _locked_experiment(
        conn,
        desired["project_id"],
        desired["key"],
    )
    if before_experiment["version"] != expected_version:
        raise VersionConflictError(
            "Experiment",
            desired["key"],
            before_experiment["version"],
        )
    if before_experiment.get("archived_at") is not None:
        raise ArchivedExperimentError(before_experiment["key"])
    if desired["flag_key"] != before_experiment["flag_key"]:
        raise IntegrityError("Experiment flag ownership is immutable")
    desired, terminal_fields = finalize_terminal_analysis_window(
        before_experiment,
        desired,
    )
    ensure_experiment_analysis_fields_immutable(
        before_experiment,
        desired,
        allowed_fields=terminal_fields,
    )
    before_flag = await _locked_flag(
        conn,
        desired["project_id"],
        before_experiment["flag_key"],
    )
    merged_flag = _derived_experiment_flag(desired, before_flag)
    updated_flag = await _update_flag(conn, merged_flag, before_flag["version"])
    updated_experiment = await _update_experiment(
        conn,
        desired,
        expected_version,
    )
    audit_action = (
        "experiment_status_changed"
        if before_experiment["status"] != updated_experiment["status"]
        else "experiment_updated"
    )
    await _audit_experiment(
        conn,
        action=audit_action,
        actor=actor,
        before=before_experiment,
        after=updated_experiment,
    )
    await _audit_flag(
        conn,
        action="flag_updated",
        actor=actor,
        origin=origin,
        before=before_flag,
        after=updated_flag,
        reason=f"experiment:{updated_experiment['key']}",
    )
    project_version = await _enqueue_flag_change(
        conn,
        "flag_updated",
        updated_flag,
    )
    await _enqueue_experiment_change(
        conn,
        "experiment_updated",
        updated_experiment,
        project_version=project_version,
    )
    return updated_experiment, updated_flag


async def update_experiment_bundle(
    pool,
    *,
    desired: dict,
    expected_version: int,
    actor: str,
) -> tuple[dict, dict]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await _update_experiment_bundle(
                conn,
                desired=desired,
                expected_version=expected_version,
                actor=actor,
                origin="experiment",
            )


async def delete_experiment_bundle(
    pool,
    *,
    project_id: str,
    key: str,
    expected_version: int,
    actor: str,
) -> tuple[dict, dict]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            experiment = await _locked_experiment(conn, project_id, key)
            if experiment["version"] != expected_version:
                raise VersionConflictError(
                    "Experiment",
                    key,
                    experiment["version"],
                )
            if experiment.get("archived_at") is not None:
                raise ArchivedExperimentError(key)
            before_flag = await _locked_flag(
                conn,
                project_id,
                experiment["flag_key"],
            )
            archived = await _archive_flag(conn, before_flag)
            if experiment["status"] == "draft":
                await _delete_draft_experiment(conn, experiment)
                removed = {
                    **experiment,
                    "version": expected_version + 1,
                }
                experiment_action = "experiment_deleted"
                audit_after = None
            else:
                removed = await _archive_experiment(
                    conn,
                    experiment,
                    actor=actor,
                )
                experiment_action = "experiment_archived"
                audit_after = removed
            await _audit_experiment(
                conn,
                action=experiment_action,
                actor=actor,
                before=experiment,
                after=audit_after,
            )
            await _audit_flag(
                conn,
                action="flag_archived",
                actor=actor,
                origin="experiment",
                before=before_flag,
                after=archived,
                reason=f"{experiment_action}:{key}",
            )
            project_version = await _enqueue_flag_change(
                conn,
                "flag_archived",
                archived,
            )
            await _enqueue_experiment_change(
                conn,
                experiment_action,
                removed,
                project_version=project_version,
            )
            return removed, archived


def _parse_timestamp(raw: str | datetime | None) -> datetime | None:
    if raw is None or isinstance(raw, datetime):
        return raw
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


async def transition_due_experiment(
    pool,
    *,
    project_id: str,
    key: str,
    expected_version: int,
    now: datetime | None = None,
) -> tuple[dict, dict] | None:
    now = now or datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await _locked_experiment(conn, project_id, key)
            if current["version"] != expected_version:
                return None
            start = _parse_timestamp(current.get("start_date"))
            end = _parse_timestamp(current.get("end_date"))
            if current["status"] == "scheduled":
                if start is None or end is None or start > now:
                    return None
                # If the scheduler missed the entire planned window, the backing
                # flag was never activated. Stop it without an analysis window
                # instead of manufacturing a completed experiment.
                target = "running" if now < end else "stopped"
                if target == "running":
                    try:
                        raw_plan = current.get("statistical_plan")
                        if isinstance(raw_plan, str):
                            raw_plan = json.loads(raw_plan)
                        plan = ExperimentStatisticalPlan.model_validate(raw_plan)
                        metric = ExperimentMetric.model_validate(
                            _load_json(current.get("primary_metric_json"), {})
                        )
                        variants = _load_json(current.get("variants_json"), [])
                        validate_statistical_plan(
                            status="running",
                            statistical_plan=plan,
                            primary_metric=metric,
                            variant_count=len(variants),
                        )
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise IntegrityError(
                            "Scheduled experiment lacks a valid predeclared statistical plan"
                        ) from exc
            elif current["status"] == "running":
                if end is None or end > now:
                    return None
                target = "completed"
            else:
                return None
            desired = {**current, "status": target}
            return await _update_experiment_bundle(
                conn,
                desired=desired,
                expected_version=expected_version,
                actor="system:experiment-scheduler",
                origin="scheduler",
            )


async def enqueue_exposure(
    pool,
    *,
    project_id: str,
    message_id: str,
    stream_key: str,
    event: dict,
) -> None:
    payload = {"stream_key": stream_key, "event": event}
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchrow(
                """
                INSERT INTO config_outbox (
                    project_id, kind, dedup_key, payload
                )
                VALUES ($1, 'exposure', $2, $3::jsonb)
                ON CONFLICT (project_id, kind, dedup_key) DO NOTHING
                RETURNING id
                """,
                project_id,
                message_id,
                _json(payload),
            )
            if inserted is not None:
                return
            existing = await conn.fetchrow(
                """
                SELECT payload
                FROM config_outbox
                WHERE project_id = $1 AND kind = 'exposure' AND dedup_key = $2
                """,
                project_id,
                message_id,
            )
            existing_payload = existing["payload"] if existing else None
            if isinstance(existing_payload, str):
                existing_payload = json.loads(existing_payload)
            if not _same_exposure_payload(existing_payload, payload):
                raise IntegrityError(
                    f"Exposure message_id '{message_id}' was reused"
                )


def _same_exposure_payload(existing: Any, requested: dict) -> bool:
    """Compare idempotent exposure retries while ignoring generated time."""
    if not isinstance(existing, dict):
        return False
    existing_copy = json.loads(_json(existing))
    requested_copy = json.loads(_json(requested))
    existing_event = existing_copy.get("event")
    requested_event = requested_copy.get("event")
    if not isinstance(existing_event, dict) or not isinstance(requested_event, dict):
        return False
    existing_event.pop("timestamp", None)
    requested_event.pop("timestamp", None)
    return existing_copy == requested_copy
