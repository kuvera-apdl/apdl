"""Private vault-to-Agents model projection contract."""

import httpx
import pytest

from app.main import app


@pytest.mark.asyncio
async def test_projection_accepts_canonical_json_array_and_requires_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "projection-token-" * 3
    monkeypatch.setenv("LLM_VAULT_PROJECTION_TOKEN", token)
    body = {
        "schema_version": "llm_vault_model_projection_request@1",
        "provider": "openai",
        "model_ids": ["gpt-5.4-mini"],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.post(
            "/internal/v1/llm-vault/project-models", json=body
        )
        allowed = await client.post(
            "/internal/v1/llm-vault/project-models",
            headers={"authorization": f"Bearer {token}"},
            json=body,
        )

    assert denied.status_code == 401
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["models"][0]["model_id"] == "gpt-5.4-mini"
