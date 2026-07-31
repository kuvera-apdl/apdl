"""Fetch deterministic consumer-specific model projections before mutation."""

from __future__ import annotations

import httpx

from app.contracts import (
    AgentsProjection,
    CodegenProjection,
    Consumer,
    ProjectModelsRequest,
    Provider,
)


class ProjectionUnavailableError(RuntimeError):
    """A consumer could not project the provider inventory safely."""


class ModelProjector:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        agents_url: str,
        codegen_url: str,
        token: str,
    ) -> None:
        self._client = client
        self._urls = {"agents": agents_url, "codegen": codegen_url}
        self._token = token

    async def project(
        self,
        consumer: Consumer,
        provider: Provider,
        model_ids: tuple[str, ...],
    ) -> AgentsProjection | CodegenProjection:
        body = ProjectModelsRequest(
            schema_version="llm_vault_model_projection_request@1",
            provider=provider,
            model_ids=list(model_ids),
        )
        try:
            response = await self._client.post(
                f"{self._urls[consumer]}/internal/v1/llm-vault/project-models",
                headers={"Authorization": f"Bearer {self._token}"},
                json=body.model_dump(mode="json"),
                timeout=10.0,
            )
            response.raise_for_status()
            projection = (
                AgentsProjection.model_validate_json(response.content)
                if consumer == "agents"
                else CodegenProjection.model_validate_json(response.content)
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise ProjectionUnavailableError(
                f"{consumer} model projection is unavailable"
            ) from exc
        if not projection.models:
            raise ProjectionUnavailableError(
                f"Provider exposes no {consumer}-eligible models"
            )
        return projection
