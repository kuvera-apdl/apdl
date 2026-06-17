"""Admin CRUD endpoints for flags and experiments."""

import json
import logging
import secrets
from datetime import date, datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.flags import experiment_flag
from app.models.schemas import (
    ExperimentCreate,
    ExperimentUpdate,
    FlagCleanup,
    FlagCreate,
    FlagDisable,
    FlagUpdate,
    GateRule,
    VariantConfig,
    derive_default_variant,
    validate_flag_variant_config,
)
from app.store import postgres as pg_store
from app.store import redis_cache
from app.utils import extract_project_id, serialize_client_flag, serialize_flag

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin")
STALE_STATE_AGE_DAYS = 90


def _unauthorized():
    return JSONResponse(
        status_code=401,
        content={"error": "unauthorized", "message": "API key or project_id required"},
    )


def _actor(request: Request) -> str:
    return request.headers.get("x-apdl-actor", "admin")


def _enabled_for_state(state: str) -> bool:
    return state == "active"


def _sync_lifecycle_update(updates: dict) -> None:
    if "state" in updates and "enabled" not in updates:
        updates["enabled"] = _enabled_for_state(updates["state"])
    elif "enabled" in updates and "state" not in updates:
        updates["state"] = "active" if updates["enabled"] else "disabled"


def _validate_merged_variant_contract(flag: dict) -> str | None:
    try:
        validate_flag_variant_config(flag)
    except (TypeError, ValueError, ValidationError) as exc:
        return str(exc)
    return None


async def _broadcast_flag_change(request: Request, project_id: str, action: str, flag: dict | None, key: str) -> None:
    payload: dict
    if (
        flag
        and flag.get("evaluation_mode") in {"client", "both"}
        and not flag.get("archived_at")
    ):
        payload = {"action": action, "flag": serialize_client_flag(flag)}
    else:
        payload = {"action": "flag_removed", "key": key}

    await request.app.state.broadcaster.broadcast(
        project_id,
        "flag_update",
        json.dumps(payload, separators=(",", ":")),
    )


def _stale_reasons(flag: dict, today: date, older_than_days: int) -> list[str]:
    reasons: list[str] = []
    owners = flag.get("owners", [])
    if not owners:
        reasons.append("missing_owner")

    review_by = _review_date(flag.get("review_by"))
    if review_by is None:
        reasons.append("missing_review_date")
    elif review_by < today:
        reasons.append("review_overdue")

    days_since_update = _days_since_update(flag)
    if flag.get("state") == "draft" and days_since_update >= older_than_days:
        reasons.append("stale_draft")
    if flag.get("state") == "disabled" and days_since_update >= older_than_days:
        reasons.append("stale_disabled")

    reasons.extend(_cleanup_reasons(flag))
    return reasons


def _cleanup_reasons(flag: dict) -> list[str]:
    if _is_cleanup_candidate(flag):
        return ["fully_rolled_out"]
    return []


def _is_cleanup_candidate(flag: dict) -> bool:
    if flag.get("state") != "active" or not flag.get("enabled", False):
        return False
    if flag.get("rules"):
        return False

    fallthrough = flag.get("fallthrough", {})
    if not isinstance(fallthrough, dict):
        return False
    rollout = fallthrough.get("rollout", {})
    if not isinstance(rollout, dict):
        return False
    try:
        percentage = float(rollout.get("percentage", 0.0))
    except (TypeError, ValueError):
        return False
    if percentage < 100.0:
        return False

    default_variant = flag.get("default_variant", "control")
    variants = flag.get("variants", [])
    if not isinstance(variants, list):
        return False

    positive_variants = [
        variant.get("key")
        for variant in variants
        if isinstance(variant, dict)
        and isinstance(variant.get("key"), str)
        and isinstance(variant.get("weight"), int)
        and variant["weight"] > 0
    ]
    return len(positive_variants) == 1 and positive_variants[0] != default_variant


def _review_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _days_since_update(flag: dict) -> int:
    updated_at = flag.get("updated_at")
    if not updated_at:
        return 0
    try:
        parsed = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return max((now - parsed.astimezone(timezone.utc)).days, 0)


# ---------- Flags ----------


@router.get("/flags")
async def list_flags(
    request: Request,
    include_archived: bool = False,
):
    """List all flags for a project."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    flags = await pg_store.get_flags(
        request.app.state.pg_pool,
        project_id,
        include_archived=include_archived,
    )
    result = [serialize_flag(flag) for flag in flags]
    return JSONResponse(content={"flags": result, "count": len(result)})


@router.get("/flags/stale")
async def list_stale_flags(
    request: Request,
    older_than_days: int = Query(default=STALE_STATE_AGE_DAYS, ge=1, le=3650),
):
    """Report flags that need owner review or rollout cleanup."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    today = date.today()
    flags = await pg_store.get_flags(request.app.state.pg_pool, project_id)
    stale_flags = []
    for flag in flags:
        reasons = _stale_reasons(flag, today, older_than_days)
        if not reasons:
            continue
        entry = serialize_flag(flag)
        entry["stale_reasons"] = reasons
        entry["cleanup_recommended"] = _is_cleanup_candidate(flag)
        entry["days_since_update"] = _days_since_update(flag)
        stale_flags.append(entry)

    return JSONResponse(content={
        "flags": stale_flags,
        "count": len(stale_flags),
        "older_than_days": older_than_days,
    })


@router.post("/flags", status_code=201)
async def create_flag(body: FlagCreate, request: Request):
    """Create a new flag. Returns 409 on duplicate, 201 on success."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    pool = request.app.state.pg_pool

    if await pg_store.get_flag(pool, project_id, body.key, include_archived=True) is not None:
        return JSONResponse(
            status_code=409,
            content={"error": "conflict", "message": f"Flag with key '{body.key}' already exists"},
        )

    body_data = body.model_dump(mode="json", exclude_none=True)
    flag = {
        **body_data,
        "project_id": project_id,
        "salt": secrets.token_urlsafe(16),
    }

    created = await pg_store.create_flag(pool, flag)
    if created is None:
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "Failed to create flag in database"},
        )

    await pg_store.create_flag_audit_entry(
        pool,
        project_id=project_id,
        flag_key=created["key"],
        action="flag_created",
        actor=_actor(request),
        before=None,
        after=created,
    )

    redis = request.app.state.redis
    await redis_cache.invalidate_flags(redis, project_id)
    await _broadcast_flag_change(request, project_id, "flag_created", created, created["key"])
    logger.info("Flag '%s' created for project %s", created["key"], project_id)
    return JSONResponse(status_code=201, content={"created": True, "flag": serialize_flag(created)})


@router.put("/flags/{key}")
async def update_flag(key: str, body: FlagUpdate, request: Request):
    """Update an existing flag (partial update). Returns 404 if not found."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    pool = request.app.state.pg_pool
    existing = await pg_store.get_flag(pool, project_id, key)
    if existing is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"Flag '{key}' not found"},
        )
    if body.version != existing["version"]:
        return JSONResponse(
            status_code=409,
            content={
                "error": "version_conflict",
                "message": f"Flag '{key}' is at version {existing['version']}",
                "current_version": existing["version"],
            },
        )

    updates = body.model_dump(exclude_unset=True, exclude_none=True, mode="json")
    updates.pop("version", None)
    _sync_lifecycle_update(updates)
    if not updates:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "message": "No flag fields provided to update"},
        )

    flag = dict(existing)
    flag.update(updates)
    variant_error = _validate_merged_variant_contract(flag)
    if variant_error is not None:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_request",
                "message": variant_error,
            },
        )

    updated = await pg_store.update_flag(pool, flag, body.version)
    if updated is None:
        return JSONResponse(
            status_code=409,
            content={
                "error": "version_conflict",
                "message": f"Flag '{key}' was modified before this update completed",
            },
        )

    await pg_store.create_flag_audit_entry(
        pool,
        project_id=project_id,
        flag_key=updated["key"],
        action="flag_updated",
        actor=_actor(request),
        before=existing,
        after=updated,
    )

    redis = request.app.state.redis
    await redis_cache.invalidate_flags(redis, project_id)
    await _broadcast_flag_change(request, project_id, "flag_updated", updated, updated["key"])
    logger.info("Flag '%s' updated for project %s", updated["key"], project_id)
    return JSONResponse(content={"updated": True, "flag": serialize_flag(updated)})


@router.post("/flags/{key}/disable")
async def disable_flag(key: str, body: FlagDisable, request: Request):
    """Disable a flag through the canonical rollback path."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    pool = request.app.state.pg_pool
    existing = await pg_store.get_flag(pool, project_id, key)
    if existing is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"Flag '{key}' not found"},
        )

    if not existing.get("enabled", False):
        return JSONResponse(content={"disabled": False, "flag": serialize_flag(existing)})

    if body.source == "system" and not existing.get("auto_disable", True):
        return JSONResponse(
            status_code=409,
            content={
                "error": "auto_disable_disabled",
                "message": f"Flag '{key}' does not allow automatic disable actions",
            },
        )

    updated = await pg_store.disable_flag(
        pool,
        project_id=project_id,
        key=key,
        reason=body.reason,
        source=body.source,
    )
    if updated is None:
        return JSONResponse(
            status_code=409,
            content={
                "error": "disable_conflict",
                "message": f"Flag '{key}' was modified before disable completed",
            },
        )

    await pg_store.create_flag_audit_entry(
        pool,
        project_id=project_id,
        flag_key=updated["key"],
        action="flag_auto_disabled" if body.source == "system" else "flag_disabled",
        actor=body.source,
        before=existing,
        after=updated,
        reason=body.reason,
        evidence=body.evidence,
    )

    redis = request.app.state.redis
    await redis_cache.invalidate_flags(redis, project_id)
    await _broadcast_flag_change(request, project_id, "flag_updated", updated, updated["key"])
    logger.warning(
        "Flag '%s' disabled for project %s by %s: %s",
        updated["key"],
        project_id,
        body.source,
        body.reason,
    )
    return JSONResponse(content={"disabled": True, "flag": serialize_flag(updated)})


@router.delete("/flags/{key}")
async def delete_flag(key: str, request: Request):
    """Delete a flag. Returns 404 if not found."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    pool = request.app.state.pg_pool
    existing = await pg_store.get_flag(pool, project_id, key)
    if existing is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"Flag '{key}' not found or already archived"},
        )

    archived = await pg_store.archive_flag(pool, project_id, key)
    if archived is None:
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "Failed to archive flag"},
        )

    await pg_store.create_flag_audit_entry(
        pool,
        project_id=project_id,
        flag_key=key,
        action="flag_archived",
        actor=_actor(request),
        before=existing,
        after=archived,
    )

    redis = request.app.state.redis
    await redis_cache.invalidate_flags(redis, project_id)
    await _broadcast_flag_change(request, project_id, "flag_archived", archived, key)
    logger.info("Flag '%s' archived for project %s", key, project_id)
    return JSONResponse(content={"archived": True, "flag": serialize_flag(archived)})


@router.post("/flags/{key}/cleanup")
async def cleanup_flag(key: str, body: FlagCleanup, request: Request):
    """Archive a fully rolled out flag through the cleanup workflow."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    pool = request.app.state.pg_pool
    existing = await pg_store.get_flag(pool, project_id, key)
    if existing is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"Flag '{key}' not found or already archived"},
        )
    if body.version != existing["version"]:
        return JSONResponse(
            status_code=409,
            content={
                "error": "version_conflict",
                "message": f"Flag '{key}' is at version {existing['version']}",
                "current_version": existing["version"],
            },
        )

    cleanup_reasons = _cleanup_reasons(existing)
    if "fully_rolled_out" not in cleanup_reasons:
        return JSONResponse(
            status_code=409,
            content={
                "error": "not_cleanup_candidate",
                "message": f"Flag '{key}' is not eligible for rollout cleanup",
                "cleanup_reasons": cleanup_reasons,
            },
        )

    archived = await pg_store.archive_flag(
        pool,
        project_id,
        key,
        expected_version=body.version,
    )
    if archived is None:
        return JSONResponse(
            status_code=409,
            content={
                "error": "version_conflict",
                "message": f"Flag '{key}' was modified before cleanup completed",
            },
        )

    evidence = {
        **body.evidence,
        "cleanup_reasons": cleanup_reasons,
    }
    await pg_store.create_flag_audit_entry(
        pool,
        project_id=project_id,
        flag_key=key,
        action="flag_cleanup_archived",
        actor=body.source,
        before=existing,
        after=archived,
        reason="fully_rolled_out",
        evidence=evidence,
    )

    redis = request.app.state.redis
    await redis_cache.invalidate_flags(redis, project_id)
    await _broadcast_flag_change(request, project_id, "flag_archived", archived, key)
    logger.info("Flag '%s' cleaned up for project %s", key, project_id)
    return JSONResponse(content={
        "cleaned_up": True,
        "cleanup_reasons": cleanup_reasons,
        "flag": serialize_flag(archived),
    })


@router.get("/flags/{key}/audit")
async def get_flag_audit(
    key: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
):
    """Return the retained audit history for a flag."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    pool = request.app.state.pg_pool
    existing = await pg_store.get_flag(pool, project_id, key, include_archived=True)
    if existing is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"Flag '{key}' not found"},
        )

    entries = await pg_store.get_flag_audit_entries(
        pool,
        project_id,
        key,
        limit=limit,
    )
    return JSONResponse(content={
        "flag_key": key,
        "audit": entries,
        "count": len(entries),
    })


# ---------- Experiments ----------

# Allowed status transitions. Same-status is permitted (editing other fields
# without a lifecycle change). 'completed' and 'stopped' are terminal — there is
# no resume (settled decision). 'draft → stopped' abandons a never-launched
# experiment.
_ALLOWED_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"draft", "running", "stopped"},
    "running": {"running", "completed", "stopped"},
    "completed": {"completed"},
    "stopped": {"stopped"},
}


def _load_json(raw, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _experiment_to_response(e: dict) -> dict:
    """Canonical experiment record returned by the list endpoint."""
    primary_metric = _load_json(e.get("primary_metric_json", "{}"), {})
    return {
        "key": e["key"],
        "flag_key": e.get("flag_key") or e["key"],
        "status": e.get("status", "draft"),
        "description": e.get("description", ""),
        "default_variant": e.get("default_variant", "control"),
        "traffic_percentage": e.get("traffic_percentage", 100.0),
        "variants": _load_json(e.get("variants_json", "[]"), []),
        "targeting_rules": _load_json(e.get("targeting_rules_json", "[]"), []),
        "primary_metric": primary_metric or None,
        "start_date": e.get("start_date", ""),
        "end_date": e.get("end_date", ""),
        "created_at": e.get("created_at", ""),
        "updated_at": e.get("updated_at", ""),
    }


def _experiment_variant_keys(variants_json: str) -> list[str]:
    variants = _load_json(variants_json, [])
    return [v["key"] for v in variants if isinstance(v, dict) and isinstance(v.get("key"), str)]


def _projected_variants(variants_json: str) -> list[VariantConfig]:
    """Project stored experiment variants down to the flag's strict {key, weight}."""
    variants = _load_json(variants_json, [])
    return [VariantConfig(key=v["key"], weight=int(v["weight"])) for v in variants]


def _experiment_rules(targeting_rules_json: str) -> list[GateRule]:
    rules = _load_json(targeting_rules_json, [])
    return [GateRule.model_validate(r) for r in rules]


def _resolve_update_default_variant(
    existing_default: str, effective_keys: list[str], body: ExperimentUpdate
) -> str:
    """Default variant after an update: an explicit choice (validated), else keep
    the existing one if still valid, else re-derive."""
    if body.default_variant is not None:
        if body.default_variant not in effective_keys:
            raise ValueError("default_variant must match a variant key")
        return body.default_variant
    if existing_default in effective_keys:
        return existing_default
    return derive_default_variant(effective_keys, None)


async def _create_experiment_flag(
    request: Request, project_id: str, flag_create: FlagCreate, actor: str
) -> dict | None:
    """Create the experiment's backing flag through the canonical flag store."""
    pool = request.app.state.pg_pool
    body_data = flag_create.model_dump(mode="json", exclude_none=True)
    flag = {**body_data, "project_id": project_id, "salt": secrets.token_urlsafe(16)}
    created = await pg_store.create_flag(pool, flag)
    if created is None:
        return None
    await pg_store.create_flag_audit_entry(
        pool,
        project_id=project_id,
        flag_key=created["key"],
        action="flag_created",
        actor=actor,
        before=None,
        after=created,
    )
    await redis_cache.invalidate_flags(request.app.state.redis, project_id)
    await _broadcast_flag_change(request, project_id, "flag_created", created, created["key"])
    return created


async def _sync_experiment_flag(
    request: Request, project_id: str, backing: dict, flag_update: FlagUpdate, actor: str
) -> tuple[dict | None, str | None]:
    """Resync the backing flag. Returns (updated_flag, validation_error)."""
    pool = request.app.state.pg_pool
    updates = flag_update.model_dump(exclude_unset=True, exclude_none=True, mode="json")
    updates.pop("version", None)
    _sync_lifecycle_update(updates)

    merged = {**backing, **updates}
    variant_error = _validate_merged_variant_contract(merged)
    if variant_error is not None:
        return None, variant_error

    updated = await pg_store.update_flag(pool, merged, backing["version"])
    if updated is None:
        return None, None

    await pg_store.create_flag_audit_entry(
        pool,
        project_id=project_id,
        flag_key=updated["key"],
        action="flag_updated",
        actor=actor,
        before=backing,
        after=updated,
    )
    await redis_cache.invalidate_flags(request.app.state.redis, project_id)
    await _broadcast_flag_change(request, project_id, "flag_updated", updated, updated["key"])
    return updated, None


async def _archive_experiment_flag(
    request: Request, project_id: str, flag_key: str, actor: str
) -> None:
    """Archive the backing flag when its experiment is deleted (never orphan it)."""
    pool = request.app.state.pg_pool
    existing = await pg_store.get_flag(pool, project_id, flag_key)
    if existing is None:
        return
    archived = await pg_store.archive_flag(pool, project_id, flag_key)
    if archived is None:
        return
    await pg_store.create_flag_audit_entry(
        pool,
        project_id=project_id,
        flag_key=flag_key,
        action="flag_archived",
        actor=actor,
        before=existing,
        after=archived,
    )
    await redis_cache.invalidate_flags(request.app.state.redis, project_id)
    await _broadcast_flag_change(request, project_id, "flag_archived", archived, flag_key)


async def _broadcast_experiment_change(
    request: Request, project_id: str, action: str, key: str, **extra
) -> None:
    payload = {"action": action, "key": key, **extra}
    await request.app.state.broadcaster.broadcast(
        project_id,
        "experiment_update",
        json.dumps(payload, separators=(",", ":")),
    )


@router.get("/experiments")
async def list_experiments(request: Request):
    """List all experiments for a project."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    experiments = await pg_store.get_experiments(request.app.state.pg_pool, project_id)
    result = [_experiment_to_response(e) for e in experiments]
    return JSONResponse(content={"experiments": result, "count": len(result)})


@router.post("/experiments", status_code=201)
async def create_experiment(body: ExperimentCreate, request: Request):
    """Create an experiment and its canonical backing flag.

    Returns 409 if the experiment key or the backing flag key already exists.
    """
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    pool = request.app.state.pg_pool
    actor = _actor(request)

    if await pg_store.get_experiment(pool, project_id, body.key) is not None:
        return JSONResponse(
            status_code=409,
            content={"error": "conflict", "message": f"Experiment with key '{body.key}' already exists"},
        )

    flag_key = body.flag_key or body.key
    if await pg_store.get_flag(pool, project_id, flag_key, include_archived=True) is not None:
        return JSONResponse(
            status_code=409,
            content={
                "error": "conflict",
                "message": f"Flag with key '{flag_key}' already exists; choose a different flag_key",
            },
        )

    variant_keys = [v.key for v in body.variants]
    default_variant = derive_default_variant(variant_keys, body.default_variant)
    flag_create = experiment_flag.build_flag_create(
        flag_key=flag_key,
        name=body.key,
        description=body.description,
        status=body.status,
        variants=[VariantConfig(key=v.key, weight=v.weight) for v in body.variants],
        default_variant=default_variant,
        traffic_percentage=body.traffic_percentage,
        targeting_rules=body.targeting_rules,
    )

    exp = {
        "key": body.key,
        "project_id": project_id,
        "status": body.status,
        "description": body.description,
        "flag_key": flag_key,
        "default_variant": default_variant,
        "variants_json": json.dumps([v.model_dump() for v in body.variants], separators=(",", ":")),
        "targeting_rules_json": json.dumps(
            [r.model_dump(mode="json") for r in body.targeting_rules], separators=(",", ":")
        ),
        "primary_metric_json": (
            json.dumps(body.primary_metric.model_dump(), separators=(",", ":"))
            if body.primary_metric is not None
            else "{}"
        ),
        "traffic_percentage": body.traffic_percentage,
        "start_date": body.start_date,
        "end_date": body.end_date,
    }

    # Create the flag first so an experiment is never persisted without one.
    created_flag = await _create_experiment_flag(request, project_id, flag_create, actor)
    if created_flag is None:
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "Failed to create backing flag"},
        )

    if not await pg_store.create_experiment(pool, exp):
        await _archive_experiment_flag(request, project_id, flag_key, actor)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "Failed to create experiment"},
        )

    await redis_cache.invalidate_experiments(request.app.state.redis, project_id)
    await _broadcast_experiment_change(
        request, project_id, "experiment_created", exp["key"], status=exp["status"], flag_key=flag_key
    )
    logger.info("Experiment '%s' created for project %s (flag '%s')", exp["key"], project_id, flag_key)
    return JSONResponse(status_code=201, content={"created": True, "key": exp["key"], "flag_key": flag_key})


@router.put("/experiments/{key}")
async def update_experiment(key: str, body: ExperimentUpdate, request: Request):
    """Update an experiment and resync its backing flag. Returns 404 if missing."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    pool = request.app.state.pg_pool
    actor = _actor(request)
    existing = await pg_store.get_experiment(pool, project_id, key)
    if existing is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"Experiment '{key}' not found"},
        )

    current_status = existing["status"]
    new_status = body.status if body.status is not None else current_status
    if new_status not in _ALLOWED_STATUS_TRANSITIONS.get(current_status, set()):
        return JSONResponse(
            status_code=409,
            content={
                "error": "invalid_transition",
                "message": f"Cannot move experiment '{key}' from '{current_status}' to '{new_status}'",
                "allowed": sorted(_ALLOWED_STATUS_TRANSITIONS.get(current_status, set())),
            },
        )

    exp = dict(existing)
    exp["status"] = new_status
    if body.description is not None:
        exp["description"] = body.description
    if body.traffic_percentage is not None:
        exp["traffic_percentage"] = body.traffic_percentage
    if body.start_date is not None:
        exp["start_date"] = body.start_date
    if body.end_date is not None:
        exp["end_date"] = body.end_date
    if body.variants is not None:
        exp["variants_json"] = json.dumps(
            [v.model_dump() for v in body.variants], separators=(",", ":")
        )
    if body.targeting_rules is not None:
        exp["targeting_rules_json"] = json.dumps(
            [r.model_dump(mode="json") for r in body.targeting_rules], separators=(",", ":")
        )
    if body.primary_metric is not None:
        exp["primary_metric_json"] = json.dumps(body.primary_metric.model_dump(), separators=(",", ":"))

    effective_keys = _experiment_variant_keys(exp["variants_json"])
    try:
        exp["default_variant"] = _resolve_update_default_variant(
            existing.get("default_variant", "control"), effective_keys, body
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "message": str(exc)},
        )

    # Resync the backing flag from the merged state before persisting the
    # experiment, so a stored experiment never describes a flag that failed to
    # apply.
    flag_key = existing.get("flag_key") or existing["key"]
    projected = _projected_variants(exp["variants_json"])
    rules = _experiment_rules(exp["targeting_rules_json"])
    backing = await pg_store.get_flag(pool, project_id, flag_key)
    if backing is None:
        flag_create = experiment_flag.build_flag_create(
            flag_key=flag_key,
            name=flag_key,
            description=exp["description"],
            status=exp["status"],
            variants=projected,
            default_variant=exp["default_variant"],
            traffic_percentage=float(exp["traffic_percentage"]),
            targeting_rules=rules,
        )
        if await _create_experiment_flag(request, project_id, flag_create, actor) is None:
            return JSONResponse(
                status_code=500,
                content={"error": "internal_error", "message": "Failed to initialize backing flag"},
            )
    else:
        flag_update = experiment_flag.build_flag_update(
            version=backing["version"],
            flag_key=flag_key,
            name=backing.get("name") or flag_key,
            description=exp["description"],
            status=exp["status"],
            variants=projected,
            default_variant=exp["default_variant"],
            traffic_percentage=float(exp["traffic_percentage"]),
            targeting_rules=rules,
        )
        updated_flag, variant_error = await _sync_experiment_flag(
            request, project_id, backing, flag_update, actor
        )
        if variant_error is not None:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "message": variant_error},
            )
        if updated_flag is None:
            return JSONResponse(
                status_code=500,
                content={"error": "internal_error", "message": "Failed to sync backing flag"},
            )

    if not await pg_store.update_experiment(pool, exp):
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "Failed to update experiment"},
        )

    await redis_cache.invalidate_experiments(request.app.state.redis, project_id)
    await _broadcast_experiment_change(
        request, project_id, "experiment_updated", exp["key"], status=exp["status"], flag_key=flag_key
    )
    logger.info("Experiment '%s' updated for project %s (flag '%s')", exp["key"], project_id, flag_key)
    return JSONResponse(content={"updated": True, "key": exp["key"], "flag_key": flag_key})


@router.delete("/experiments/{key}")
async def delete_experiment(key: str, request: Request):
    """Delete an experiment and archive its backing flag. Returns 404 if missing."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    pool = request.app.state.pg_pool
    actor = _actor(request)
    existing = await pg_store.get_experiment(pool, project_id, key)
    if existing is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"Experiment '{key}' not found or already deleted"},
        )

    flag_key = existing.get("flag_key") or existing["key"]
    if not await pg_store.delete_experiment(pool, project_id, key):
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"Experiment '{key}' not found or already deleted"},
        )

    await _archive_experiment_flag(request, project_id, flag_key, actor)

    await redis_cache.invalidate_experiments(request.app.state.redis, project_id)
    await _broadcast_experiment_change(
        request, project_id, "experiment_deleted", key, flag_key=flag_key
    )
    logger.info("Experiment '%s' deleted for project %s (flag '%s' archived)", key, project_id, flag_key)
    return JSONResponse(content={"deleted": True, "key": key, "flag_key": flag_key})
