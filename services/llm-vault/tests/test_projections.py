from __future__ import annotations

import httpx
import pytest

from app.projections import ModelProjector, ProjectionUnavailableError


@pytest.mark.asyncio
async def test_projects_only_through_consumer_catalog_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer projection-token"
        assert request.url.path == "/internal/v1/llm-vault/project-models"
        return httpx.Response(
            200,
            json={
                "schema_version": "agents_llm_model_projection@1",
                "catalog_version": "catalog@1",
                "models": [
                    {
                        "schema_version": "llm_provider_model@1",
                        "provider": "openai",
                        "model_id": "gpt-5.4-mini",
                        "display_name": "GPT-5.4 Mini",
                        "supported_tiers": ["fast", "reasoning"],
                        "catalog_version": "catalog@1",
                        "data_residency": "global",
                        "allowed_data_classifications": ["public", "internal"],
                        "pricing_status": "catalog_reviewed",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        projector = ModelProjector(
            client,
            agents_url="http://agents.test",
            codegen_url="http://codegen.test",
            token="projection-token",
        )
        result = await projector.project(
            "agents", "openai", ("gpt-5.4-mini",)
        )
    assert result.models[0].model_id == "gpt-5.4-mini"


@pytest.mark.asyncio
async def test_rejects_empty_or_malformed_projection() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": "agents_llm_model_projection@1",
                "catalog_version": "catalog@1",
                "models": [],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        projector = ModelProjector(
            client,
            agents_url="http://agents.test",
            codegen_url="http://codegen.test",
            token="projection-token",
        )
        with pytest.raises(ProjectionUnavailableError):
            await projector.project("agents", "openai", ("unknown",))
