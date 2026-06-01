"""Admin CRUD endpoints for flags and experiments."""

import json
import logging
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.models.schemas import (
    ExperimentCreate,
    ExperimentUpdate,
    FlagCreate,
    FlagDisable,
    FlagUpdate,
)
from app.store import postgres as pg_store
from app.store import redis_cache
from app.utils import extract_project_id, serialize_client_flag, serialize_flag

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin")


def _unauthorized():
    return JSONResponse(
        status_code=401,
        content={"error": "unauthorized", "message": "API key or project_id required"},
    )


def _actor(request: Request) -> str:
    return request.headers.get("x-apdl-actor", "admin")


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


# ---------- Flags ----------


@router.get("/flags")
async def list_flags(request: Request):
    """List all flags for a project."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    flags = await pg_store.get_flags(request.app.state.pg_pool, project_id)
    result = [serialize_flag(flag) for flag in flags]
    return JSONResponse(content={"flags": result, "count": len(result)})


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
    if not updates:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "message": "No flag fields provided to update"},
        )

    flag = dict(existing)
    flag.update(updates)

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


# ---------- Experiments ----------


@router.get("/experiments")
async def list_experiments(request: Request):
    """List all experiments for a project."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    experiments = await pg_store.get_experiments(request.app.state.pg_pool, project_id)
    result = []
    for e in experiments:
        entry: dict = {
            "key": e["key"],
            "status": e.get("status", "draft"),
            "description": e.get("description", ""),
            "traffic_percentage": e.get("traffic_percentage", 100.0),
        }
        variants_json = e.get("variants_json", "[]")
        if variants_json and variants_json != "[]":
            entry["variants"] = json.loads(variants_json)
        entry["start_date"] = e.get("start_date", "")
        entry["end_date"] = e.get("end_date", "")
        entry["created_at"] = e.get("created_at", "")
        entry["updated_at"] = e.get("updated_at", "")
        result.append(entry)

    return JSONResponse(content={"experiments": result, "count": len(result)})


@router.post("/experiments", status_code=201)
async def create_experiment(body: ExperimentCreate, request: Request):
    """Create a new experiment. Returns 409 on duplicate, 201 on success."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    pool = request.app.state.pg_pool

    if await pg_store.get_experiment(pool, project_id, body.key) is not None:
        return JSONResponse(
            status_code=409,
            content={"error": "conflict", "message": f"Experiment with key '{body.key}' already exists"},
        )

    exp = {
        "key": body.key,
        "project_id": project_id,
        "status": body.status,
        "description": body.description,
        "traffic_percentage": body.traffic_percentage,
        "start_date": body.start_date,
        "end_date": body.end_date,
        "variants_json": json.dumps(body.variants, separators=(",", ":")),
        "targeting_rules_json": json.dumps(body.targeting_rules, separators=(",", ":")),
    }

    if not await pg_store.create_experiment(pool, exp):
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "Failed to create experiment"},
        )

    redis = request.app.state.redis
    await redis_cache.invalidate_experiments(redis, project_id)
    await request.app.state.broadcaster.broadcast(
        project_id,
        "experiment_update",
        json.dumps({"action": "experiment_created", "key": exp["key"], "status": exp["status"]}, separators=(",", ":")),
    )
    logger.info("Experiment '%s' created for project %s", exp["key"], project_id)
    return JSONResponse(status_code=201, content={"created": True, "key": exp["key"]})


@router.put("/experiments/{key}")
async def update_experiment(key: str, body: ExperimentUpdate, request: Request):
    """Update an existing experiment (partial update). Returns 404 if not found."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    pool = request.app.state.pg_pool
    existing = await pg_store.get_experiment(pool, project_id, key)
    if existing is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"Experiment '{key}' not found"},
        )

    exp = dict(existing)
    if body.status is not None:
        exp["status"] = body.status
    if body.description is not None:
        exp["description"] = body.description
    if body.traffic_percentage is not None:
        exp["traffic_percentage"] = body.traffic_percentage
    if body.start_date is not None:
        exp["start_date"] = body.start_date
    if body.end_date is not None:
        exp["end_date"] = body.end_date
    if body.variants is not None:
        exp["variants_json"] = json.dumps(body.variants, separators=(",", ":"))
    if body.targeting_rules is not None:
        exp["targeting_rules_json"] = json.dumps(body.targeting_rules, separators=(",", ":"))

    if not await pg_store.update_experiment(pool, exp):
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "Failed to update experiment"},
        )

    redis = request.app.state.redis
    await redis_cache.invalidate_experiments(redis, project_id)
    await request.app.state.broadcaster.broadcast(
        project_id,
        "experiment_update",
        json.dumps({"action": "experiment_updated", "key": exp["key"], "status": exp["status"]}, separators=(",", ":")),
    )
    logger.info("Experiment '%s' updated for project %s", exp["key"], project_id)
    return JSONResponse(content={"updated": True, "key": exp["key"]})


@router.delete("/experiments/{key}")
async def delete_experiment(key: str, request: Request):
    """Delete an experiment. Returns 404 if not found."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    if not await pg_store.delete_experiment(request.app.state.pg_pool, project_id, key):
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"Experiment '{key}' not found or already deleted"},
        )

    redis = request.app.state.redis
    await redis_cache.invalidate_experiments(redis, project_id)
    await request.app.state.broadcaster.broadcast(
        project_id,
        "experiment_update",
        json.dumps({"action": "experiment_deleted", "key": key}, separators=(",", ":")),
    )
    logger.info("Experiment '%s' deleted for project %s", key, project_id)
    return JSONResponse(content={"deleted": True, "key": key})
