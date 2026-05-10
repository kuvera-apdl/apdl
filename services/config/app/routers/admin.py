"""Admin CRUD endpoints for flags and experiments."""

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.models.schemas import (
    ExperimentCreate,
    ExperimentUpdate,
    FlagCreate,
    FlagUpdate,
)
from app.store import postgres as pg_store
from app.store import redis_cache
from app.utils import extract_project_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin")


def _unauthorized():
    return JSONResponse(
        status_code=401,
        content={"error": "unauthorized", "message": "API key or project_id required"},
    )


# ---------- Flags ----------


@router.get("/flags")
async def list_flags(request: Request):
    """List all flags for a project."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    flags = await pg_store.get_flags(request.app.state.pg_pool, project_id)
    result = [
        {
            "key": f["key"],
            "enabled": f["enabled"],
            "description": f.get("description", ""),
            "variant_type": f.get("variant_type", "boolean"),
            "default_value": f.get("default_value", "false"),
            "rollout_percentage": f.get("rollout_percentage", 100.0),
            "created_at": f.get("created_at", ""),
            "updated_at": f.get("updated_at", ""),
        }
        for f in flags
    ]
    return JSONResponse(content={"flags": result, "count": len(result)})


@router.post("/flags", status_code=201)
async def create_flag(body: FlagCreate, request: Request):
    """Create a new flag. Returns 409 on duplicate, 201 on success."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    pool = request.app.state.pg_pool

    if await pg_store.get_flag(pool, project_id, body.key) is not None:
        return JSONResponse(
            status_code=409,
            content={"error": "conflict", "message": f"Flag with key '{body.key}' already exists"},
        )

    flag = {
        "key": body.key,
        "project_id": project_id,
        "enabled": body.enabled,
        "description": body.description,
        "variant_type": body.variant_type,
        "default_value": body.default_value,
        "rollout_percentage": body.rollout_percentage,
        "rules_json": json.dumps(body.rules, separators=(",", ":")),
        "variants_json": json.dumps(body.variants, separators=(",", ":")),
    }

    if not await pg_store.create_flag(pool, flag):
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "Failed to create flag in database"},
        )

    redis = request.app.state.redis
    await redis_cache.invalidate_flags(redis, project_id)
    await request.app.state.broadcaster.broadcast(
        project_id,
        "flag_update",
        json.dumps({"action": "flag_created", "key": flag["key"], "enabled": flag["enabled"]}, separators=(",", ":")),
    )
    logger.info("Flag '%s' created for project %s", flag["key"], project_id)
    return JSONResponse(status_code=201, content={"created": True, "key": flag["key"]})


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

    flag = dict(existing)
    if body.enabled is not None:
        flag["enabled"] = body.enabled
    if body.description is not None:
        flag["description"] = body.description
    if body.variant_type is not None:
        flag["variant_type"] = body.variant_type
    if body.default_value is not None:
        flag["default_value"] = body.default_value
    if body.rollout_percentage is not None:
        flag["rollout_percentage"] = body.rollout_percentage
    if body.rules is not None:
        flag["rules_json"] = json.dumps(body.rules, separators=(",", ":"))
    if body.variants is not None:
        flag["variants_json"] = json.dumps(body.variants, separators=(",", ":"))

    if not await pg_store.update_flag(pool, flag):
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "Failed to update flag"},
        )

    redis = request.app.state.redis
    await redis_cache.invalidate_flags(redis, project_id)
    await request.app.state.broadcaster.broadcast(
        project_id,
        "flag_update",
        json.dumps({"action": "flag_updated", "key": flag["key"], "enabled": flag["enabled"]}, separators=(",", ":")),
    )
    logger.info("Flag '%s' updated for project %s", flag["key"], project_id)
    return JSONResponse(content={"updated": True, "key": flag["key"]})


@router.delete("/flags/{key}")
async def delete_flag(key: str, request: Request):
    """Delete a flag. Returns 404 if not found."""
    project_id = extract_project_id(request)
    if not project_id:
        return _unauthorized()

    if not await pg_store.delete_flag(request.app.state.pg_pool, project_id, key):
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"Flag '{key}' not found or already deleted"},
        )

    redis = request.app.state.redis
    await redis_cache.invalidate_flags(redis, project_id)
    await request.app.state.broadcaster.broadcast(
        project_id,
        "flag_update",
        json.dumps({"action": "flag_deleted", "key": key}, separators=(",", ":")),
    )
    logger.info("Flag '%s' deleted for project %s", key, project_id)
    return JSONResponse(content={"deleted": True, "key": key})


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
