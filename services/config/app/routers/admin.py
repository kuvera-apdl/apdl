"""Admin CRUD endpoints for flags and experiments."""

import hashlib
import json
import logging
import re
import secrets
from datetime import date, datetime, timezone

import asyncpg
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.auth import authorized_project
from app.flags import experiment_flag
from app.models.schemas import (
    ExperimentCreate,
    ExperimentMetric,
    ExperimentStatisticalPlan,
    ExperimentUpdate,
    FlagCleanup,
    FlagCreate,
    FlagDisable,
    FlagTransition,
    FlagUpdate,
    VariantConfig,
    validate_experiment_lifecycle,
    validate_statistical_plan,
)
from app.store import postgres as pg_store
from app.store import mutations
from app.utils import serialize_flag

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin")
STALE_STATE_AGE_DAYS = 90
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


def _experiment_creation_request_sha256(
    project_id: str,
    body: ExperimentCreate,
) -> str:
    canonical = json.dumps(
        {
            "project_id": project_id,
            "experiment": body.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _idempotent_experiment_response(existing: dict) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={
            "created": True,
            "key": existing["key"],
            "flag_key": existing["flag_key"],
            "bucket_by": existing["bucket_by"],
            "version": existing["version"],
        },
    )



def _actor(request: Request) -> str:
    return f"credential:{request.state.principal.credential_id}"


def _mutation_error(exc: mutations.MutationError) -> JSONResponse:
    if isinstance(exc, mutations.NotFoundError):
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": str(exc)},
        )
    if isinstance(exc, mutations.ExperimentOwnedFlagError):
        return JSONResponse(
            status_code=409,
            content={
                "error": "experiment_managed_flag",
                "message": str(exc),
                "experiment_key": exc.experiment_key,
            },
        )
    if isinstance(exc, mutations.VersionConflictError):
        return JSONResponse(
            status_code=409,
            content={
                "error": "version_conflict",
                "message": str(exc),
                "current_version": exc.current_version,
            },
        )
    if isinstance(exc, mutations.ImmutableExperimentError):
        return JSONResponse(
            status_code=409,
            content={
                "error": "immutable_experiment_contract",
                "message": str(exc),
                "fields": exc.fields,
            },
        )
    if isinstance(exc, mutations.ArchivedExperimentError):
        return JSONResponse(
            status_code=409,
            content={
                "error": "experiment_archived",
                "message": str(exc),
            },
        )
    return JSONResponse(
        status_code=409,
        content={"error": "conflict", "message": str(exc)},
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
    project_id = authorized_project(request, "config:write")

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
    project_id = authorized_project(request, "config:write")

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
    project_id = authorized_project(request, "config:write")

    body_data = body.model_dump(mode="json", exclude_none=True)
    flag = {
        **body_data,
        "project_id": project_id,
        "salt": secrets.token_urlsafe(16),
    }

    try:
        created = await mutations.create_standalone_flag(
            request.app.state.pg_pool,
            flag,
            actor=_actor(request),
        )
    except asyncpg.UniqueViolationError:
        return JSONResponse(
            status_code=409,
            content={
                "error": "conflict",
                "message": f"Flag with key '{body.key}' already exists",
            },
        )
    logger.info("Flag '%s' created for project %s", created["key"], project_id)
    return JSONResponse(status_code=201, content={"created": True, "flag": serialize_flag(created)})


@router.put("/flags/{key}")
async def update_flag(key: str, body: FlagUpdate, request: Request):
    """Update an existing flag (partial update). Returns 404 if not found."""
    project_id = authorized_project(request, "config:write")

    updates = body.model_dump(exclude_unset=True, mode="json")
    updates.pop("version", None)
    if not updates:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "message": "No flag fields provided to update"},
        )

    try:
        updated = await mutations.update_standalone_flag(
            request.app.state.pg_pool,
            project_id=project_id,
            key=key,
            expected_version=body.version,
            updates=updates,
            actor=_actor(request),
        )
    except mutations.MutationError as exc:
        return _mutation_error(exc)
    logger.info("Flag '%s' updated for project %s", updated["key"], project_id)
    return JSONResponse(content={"updated": True, "flag": serialize_flag(updated)})


@router.post("/flags/{key}/transition")
async def transition_flag(key: str, body: FlagTransition, request: Request):
    """Use the dedicated lifecycle path for draft/active transitions."""
    project_id = authorized_project(request, "config:write")
    try:
        updated = await mutations.transition_standalone_flag(
            request.app.state.pg_pool,
            project_id=project_id,
            key=key,
            expected_version=body.version,
            target_state=body.target_state,
            actor=_actor(request),
        )
    except mutations.MutationError as exc:
        return _mutation_error(exc)
    return JSONResponse(content={"updated": True, "flag": serialize_flag(updated)})


@router.post("/flags/{key}/disable")
async def disable_flag(key: str, body: FlagDisable, request: Request):
    """Disable a flag through the canonical rollback path."""
    project_id = authorized_project(request, "config:write")

    try:
        updated, changed = await mutations.disable_standalone_flag(
            request.app.state.pg_pool,
            project_id=project_id,
            key=key,
            expected_version=body.version,
            reason=body.reason,
            evidence=body.evidence,
            actor=_actor(request),
        )
    except mutations.MutationError as exc:
        return _mutation_error(exc)
    logger.warning(
        "Flag '%s' disabled for project %s by %s: %s",
        updated["key"],
        project_id,
        _actor(request),
        body.reason,
    )
    return JSONResponse(
        content={"disabled": changed, "flag": serialize_flag(updated)}
    )


@router.delete("/flags/{key}")
async def delete_flag(
    key: str,
    request: Request,
    version: int = Query(..., ge=1),
):
    """Delete a flag. Returns 404 if not found."""
    project_id = authorized_project(request, "config:write")

    try:
        archived = await mutations.archive_standalone_flag(
            request.app.state.pg_pool,
            project_id=project_id,
            key=key,
            expected_version=version,
            actor=_actor(request),
        )
    except mutations.MutationError as exc:
        return _mutation_error(exc)
    logger.info("Flag '%s' archived for project %s", key, project_id)
    return JSONResponse(content={"archived": True, "flag": serialize_flag(archived)})


@router.post("/flags/{key}/cleanup")
async def cleanup_flag(key: str, body: FlagCleanup, request: Request):
    """Archive a fully rolled out flag through the cleanup workflow."""
    project_id = authorized_project(request, "config:write")

    try:
        archived, cleanup_reasons = await mutations.cleanup_standalone_flag(
            request.app.state.pg_pool,
            project_id=project_id,
            key=key,
            expected_version=body.version,
            evidence=body.evidence,
            actor=_actor(request),
        )
    except mutations.IntegrityError as exc:
        return JSONResponse(
            status_code=409,
            content={"error": "not_cleanup_candidate", "message": str(exc)},
        )
    except mutations.MutationError as exc:
        return _mutation_error(exc)
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
    project_id = authorized_project(request, "config:write")

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
    "draft": {"draft", "scheduled", "running", "stopped"},
    "scheduled": {"scheduled", "running", "stopped"},
    "running": {"running", "completed", "stopped"},
    "completed": {"completed"},
    "stopped": {"stopped"},
}
_FROZEN_EXPERIMENT_REQUEST_FIELDS = frozenset(
    {
        "bucket_by",
        "default_variant",
        "traffic_percentage",
        "targeting_rules",
        "variants",
        "primary_metric",
        "statistical_plan",
        "start_date",
        "end_date",
    }
)


def _load_json(raw, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _as_datetime(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _iso_or_none(value) -> str | None:
    parsed = _as_datetime(value)
    return parsed.isoformat() if parsed is not None else None


def _metric_or_none(value: dict) -> ExperimentMetric | None:
    return ExperimentMetric.model_validate(value) if value else None


def _statistical_plan_or_none(value) -> ExperimentStatisticalPlan | None:
    return ExperimentStatisticalPlan.model_validate(value) if value else None


def _experiment_to_response(e: dict) -> dict:
    """Canonical experiment record returned by the list endpoint."""
    primary_metric = _load_json(e.get("primary_metric_json", "{}"), {})
    return {
        "key": e["key"],
        "flag_key": e.get("flag_key") or e["key"],
        "bucket_by": e["bucket_by"],
        "status": e.get("status", "draft"),
        "description": e.get("description", ""),
        "default_variant": e.get("default_variant", "control"),
        "traffic_percentage": e.get("traffic_percentage", 100.0),
        "variants": _load_json(e.get("variants_json", "[]"), []),
        "targeting_rules": _load_json(e.get("targeting_rules_json", "[]"), []),
        "primary_metric": primary_metric or None,
        "statistical_plan": e.get("statistical_plan"),
        "start_date": _iso_or_none(e.get("start_date")),
        "end_date": _iso_or_none(e.get("end_date")),
        "version": e.get("version", 1),
        "created_at": _iso_or_none(e.get("created_at")),
        "updated_at": _iso_or_none(e.get("updated_at")),
        "archived_at": _iso_or_none(e.get("archived_at")),
        "archived_by": e.get("archived_by"),
    }


def _experiment_variant_keys(variants_json: str) -> list[str]:
    variants = _load_json(variants_json, [])
    return [v["key"] for v in variants if isinstance(v, dict) and isinstance(v.get("key"), str)]


def _resolve_update_default_variant(
    existing_default: str, effective_keys: list[str], body: ExperimentUpdate
) -> str:
    """Resolve the explicitly authored default without inventing a fallback."""
    effective_default = body.default_variant or existing_default
    if effective_default not in effective_keys:
        raise ValueError("default_variant must match a variant key")
    return effective_default


@router.get("/experiments")
async def list_experiments(request: Request):
    """List all experiments for a project."""
    project_id = authorized_project(request, "config:write")

    experiments = await pg_store.get_experiments(request.app.state.pg_pool, project_id)
    result = [_experiment_to_response(e) for e in experiments]
    return JSONResponse(content={"experiments": result, "count": len(result)})


@router.post("/experiments", status_code=201)
async def create_experiment(body: ExperimentCreate, request: Request):
    """Create an experiment and its canonical backing flag.

    Returns 409 if the experiment key or the backing flag key already exists.
    """
    project_id = authorized_project(request, "config:write")

    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key is not None and _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key) is None:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_idempotency_key",
                "message": "Idempotency-Key must be a canonical 1 to 200 character key",
            },
        )

    idempotency_request_sha256 = (
        _experiment_creation_request_sha256(project_id, body)
        if idempotency_key is not None
        else None
    )
    if idempotency_key is not None:
        existing = await pg_store.get_experiment_by_creation_idempotency_key(
            request.app.state.pg_pool,
            project_id,
            idempotency_key,
        )
        if existing is not None:
            if existing["creation_idempotency_request_sha256"] != idempotency_request_sha256:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "idempotency_conflict",
                        "message": (
                            "Idempotency-Key is bound to a different experiment request"
                        ),
                    },
                )
            return _idempotent_experiment_response(existing)

    actor = _actor(request)

    flag_key = body.flag_key or body.key

    flag_create = experiment_flag.build_flag_create(
        flag_key=flag_key,
        name=body.key,
        description=body.description,
        status=body.status,
        variants=[VariantConfig(key=v.key, weight=v.weight) for v in body.variants],
        default_variant=body.default_variant,
        traffic_percentage=body.traffic_percentage,
        targeting_rules=body.targeting_rules,
        bucket_by=body.bucket_by,
    )

    exp = {
        "key": body.key,
        "project_id": project_id,
        "status": body.status,
        "description": body.description,
        "flag_key": flag_key,
        "bucket_by": body.bucket_by,
        "default_variant": body.default_variant,
        "variants_json": json.dumps([v.model_dump() for v in body.variants], separators=(",", ":")),
        "targeting_rules_json": json.dumps(
            [
                r.model_dump(mode="json", exclude_none=True)
                for r in body.targeting_rules
            ],
            separators=(",", ":"),
        ),
        "primary_metric_json": (
            json.dumps(body.primary_metric.model_dump(), separators=(",", ":"))
            if body.primary_metric is not None
            else "{}"
        ),
        "statistical_plan": (
            body.statistical_plan.model_dump(mode="json")
            if body.statistical_plan is not None
            else None
        ),
        "traffic_percentage": body.traffic_percentage,
        "start_date": body.start_date,
        "end_date": body.end_date,
        "creation_idempotency_key": idempotency_key,
        "creation_idempotency_request_sha256": idempotency_request_sha256,
    }
    flag = {
        **flag_create.model_dump(mode="json", exclude_none=True),
        "project_id": project_id,
        "salt": secrets.token_urlsafe(16),
    }
    try:
        created_exp, _ = await mutations.create_experiment_bundle(
            request.app.state.pg_pool,
            experiment=exp,
            flag=flag,
            actor=actor,
        )
    except asyncpg.UniqueViolationError:
        if idempotency_key is not None:
            existing = await pg_store.get_experiment_by_creation_idempotency_key(
                request.app.state.pg_pool,
                project_id,
                idempotency_key,
            )
            if existing is not None:
                if existing["creation_idempotency_request_sha256"] != idempotency_request_sha256:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "error": "idempotency_conflict",
                            "message": (
                                "Idempotency-Key is bound to a different experiment request"
                            ),
                        },
                    )
                return _idempotent_experiment_response(existing)
        return JSONResponse(
            status_code=409,
            content={
                "error": "conflict",
                "message": (
                    f"Experiment '{body.key}' or flag '{flag_key}' already exists"
                ),
            },
        )
    logger.info("Experiment '%s' created for project %s (flag '%s')", exp["key"], project_id, flag_key)
    return JSONResponse(
        status_code=201,
        content={
            "created": True,
            "key": exp["key"],
            "flag_key": flag_key,
            "bucket_by": created_exp["bucket_by"],
            "version": created_exp["version"],
        },
    )


@router.put("/experiments/{key}")
async def update_experiment(key: str, body: ExperimentUpdate, request: Request):
    """Update an experiment and resync its backing flag. Returns 404 if missing."""
    project_id = authorized_project(request, "config:write")

    pool = request.app.state.pg_pool
    actor = _actor(request)
    existing = await pg_store.get_experiment(pool, project_id, key)
    if existing is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"Experiment '{key}' not found"},
        )
    if body.version != existing["version"]:
        return _mutation_error(
            mutations.VersionConflictError(
                "Experiment",
                key,
                existing["version"],
            )
        )
    if existing.get("archived_at") is not None:
        return _mutation_error(mutations.ArchivedExperimentError(key))

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

    if current_status != "draft":
        frozen_fields = sorted(
            body.model_fields_set & _FROZEN_EXPERIMENT_REQUEST_FIELDS
        )
        if frozen_fields:
            return _mutation_error(
                mutations.ImmutableExperimentError(key, frozen_fields)
            )

    exp = dict(existing)
    exp["status"] = new_status
    if body.description is not None:
        exp["description"] = body.description
    if body.bucket_by is not None:
        exp["bucket_by"] = body.bucket_by
    if body.traffic_percentage is not None:
        exp["traffic_percentage"] = body.traffic_percentage
    if "start_date" in body.model_fields_set:
        exp["start_date"] = body.start_date
    if "end_date" in body.model_fields_set:
        exp["end_date"] = body.end_date
    if body.variants is not None:
        exp["variants_json"] = json.dumps(
            [v.model_dump() for v in body.variants], separators=(",", ":")
        )
    if body.targeting_rules is not None:
        exp["targeting_rules_json"] = json.dumps(
            [
                r.model_dump(mode="json", exclude_none=True)
                for r in body.targeting_rules
            ],
            separators=(",", ":"),
        )
    if "primary_metric" in body.model_fields_set:
        exp["primary_metric_json"] = (
            json.dumps(
                body.primary_metric.model_dump(mode="json"),
                separators=(",", ":"),
            )
            if body.primary_metric is not None
            else "{}"
        )
    if "statistical_plan" in body.model_fields_set:
        exp["statistical_plan"] = (
            body.statistical_plan.model_dump(mode="json")
            if body.statistical_plan is not None
            else None
        )

    primary_metric_data = _load_json(exp.get("primary_metric_json", "{}"), {})
    try:
        validate_experiment_lifecycle(
            status=exp["status"],
            start_date=_as_datetime(exp.get("start_date")),
            end_date=_as_datetime(exp.get("end_date")),
            primary_metric=_metric_or_none(primary_metric_data),
        )
        validate_statistical_plan(
            status=exp["status"],
            statistical_plan=_statistical_plan_or_none(
                exp.get("statistical_plan")
            ),
            primary_metric=_metric_or_none(primary_metric_data),
            variant_count=len(_experiment_variant_keys(exp["variants_json"])),
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "message": str(exc)},
        )

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

    flag_key = existing.get("flag_key") or existing["key"]
    try:
        updated_exp, _ = await mutations.update_experiment_bundle(
            pool,
            desired=exp,
            expected_version=body.version,
            actor=actor,
        )
    except mutations.MutationError as exc:
        return _mutation_error(exc)
    logger.info("Experiment '%s' updated for project %s (flag '%s')", exp["key"], project_id, flag_key)
    return JSONResponse(
        content={
            "updated": True,
            "key": exp["key"],
            "flag_key": flag_key,
            "bucket_by": updated_exp["bucket_by"],
            "version": updated_exp["version"],
        }
    )


@router.delete("/experiments/{key}")
async def delete_experiment(
    key: str,
    request: Request,
    version: int = Query(..., ge=1),
):
    """Delete a draft or archive a launched experiment and its backing flag."""
    project_id = authorized_project(request, "config:write")

    actor = _actor(request)
    try:
        deleted, _ = await mutations.delete_experiment_bundle(
            request.app.state.pg_pool,
            project_id=project_id,
            key=key,
            expected_version=version,
            actor=actor,
        )
    except mutations.MutationError as exc:
        return _mutation_error(exc)
    flag_key = deleted["flag_key"]
    archived = deleted.get("archived_at") is not None
    logger.info(
        "Experiment '%s' %s for project %s (flag '%s' archived)",
        key,
        "archived" if archived else "deleted",
        project_id,
        flag_key,
    )
    return JSONResponse(
        content={
            "deleted": not archived,
            "archived": archived,
            "key": key,
            "flag_key": flag_key,
            "version": deleted["version"],
        }
    )


@router.get("/experiments/{key}/audit")
async def get_experiment_audit(
    key: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
):
    """Return lifecycle evidence retained across deletion and archival."""
    project_id = authorized_project(request, "config:write")
    entries = await pg_store.get_experiment_audit_entries(
        request.app.state.pg_pool,
        project_id,
        key,
        limit=limit,
    )
    return JSONResponse(
        content={
            "experiment_key": key,
            "audit": entries,
            "count": len(entries),
        }
    )
