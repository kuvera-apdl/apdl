"""Read-only authoritative experiment contracts."""

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import JSONResponse

from app.auth import authorized_project
from app.experiments.analysis import (
    ExperimentNotAnalyzableError,
    build_analysis_contract,
)
from app.models.schemas import ExperimentAnalysis, RESOURCE_KEY_PATTERN
from app.store import postgres as pg_store


router = APIRouter()


@router.get(
    "/v1/experiments/{key}/analysis",
    response_model=ExperimentAnalysis,
)
async def get_experiment_analysis(
    request: Request,
    key: str = Path(..., pattern=RESOURCE_KEY_PATTERN),
):
    """Return one tenant-scoped, analysis-ready experiment contract."""
    if request.query_params:
        raise HTTPException(
            status_code=422,
            detail=(
                "Unknown query parameter(s): "
                + ", ".join(sorted(set(request.query_params)))
            ),
        )
    project_id = authorized_project(request, "query:read")
    experiment = await pg_store.get_experiment(
        request.app.state.pg_pool,
        project_id,
        key,
    )
    if experiment is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "not_found",
                "message": f"Experiment '{key}' not found",
            },
        )
    try:
        return build_analysis_contract(experiment)
    except ExperimentNotAnalyzableError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": "experiment_not_analyzable",
                "message": str(exc),
            },
        )
